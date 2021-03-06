#
# Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import contextlib
import functools
import logging
import math

import distiller
import distiller.quantization
import examples.automated_deep_compression as adc
from distiller.utils import float_range_argparse_checker as float_range
import distiller.models as models

msglogger = logging.getLogger()

SUMMARY_CHOICES = ['sparsity', 'compute', 'model', 'modules', 'png', 'png_w_params', 'onnx']
DEFAULT_PRINT_FREQUENCY = 10
DEFAULT_LOADERS_COUNT = 5


def get_parser():
    parser = argparse.ArgumentParser(description='Distiller image classification model compression')
    parser.add_argument('data', metavar='DIR', help='path to dataset')
    parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18', type=lambda s: s.lower(),
                        choices=models.ALL_MODEL_NAMES,
                        help='model architecture: ' +
                        ' | '.join(models.ALL_MODEL_NAMES) +
                        ' (default: resnet18)')
    parser.add_argument('--loaders', type=int, metavar='N',
                        help='number of data loading workers (default: max({}, {} per GPU). 1 if deterministic is set.)'.format(
                            DEFAULT_LOADERS_COUNT, DEFAULT_LOADERS_COUNT))
    parser.add_argument('--epochs', default=90, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-b', '--batch-size', default=256, type=int,
                        metavar='N', help='mini-batch size (default: 256)')

    optimizer_args = parser.add_argument_group('optimizer_arguments')
    optimizer_args.add_argument('--lr', '--learning-rate', default=0.1,
                        type=float, metavar='LR', help='initial learning rate')
    optimizer_args.add_argument('--momentum', default=0.9, type=float,
                        metavar='M', help='momentum')
    optimizer_args.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)')
    parser.add_argument('--reset-optimizer', '--reset-lr', action='store_true',
                        help='Flag to override optimizer if resumed from checkpoint')

    print_freq_group = parser.add_mutually_exclusive_group()
    print_freq_group.add_argument('--print-frequency', type=int, metavar='N',
                        help='print frequency (default: {} prints per epoch)'.format(DEFAULT_PRINT_FREQUENCY))
    print_freq_group.add_argument('--print-period', type=int,
                        metavar='N', help='print every N mini-batches')

    load_checkpoint_group = parser.add_mutually_exclusive_group()
    load_checkpoint_group.add_argument('--resume', default='', type=str, metavar='PATH',
                        help='path to latest checkpoint (default: none)')
    load_checkpoint_group.add_argument('--load-state-dict', default='', type=str, metavar='PATH',
                        help='load only state dict field from checkpoint at given path')

    parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                        help='evaluate model on validation set')
    parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                        help='use pre-trained model')
    parser.add_argument('--activation-stats', '--act-stats', nargs='+', metavar='PHASE', default=list(),
                        help='collect activation statistics on phases: train, valid, and/or test'
                        ' (WARNING: this slows down training)')
    parser.add_argument('--masks-sparsity', dest='masks_sparsity', action='store_true', default=False,
                        help='print masks sparsity table at end of each epoch')
    parser.add_argument('--param-hist', dest='log_params_histograms', action='store_true', default=False,
                        help='log the parameter tensors histograms to file (WARNING: this can use significant disk space)')
    parser.add_argument('--summary', type=lambda s: s.lower(), choices=SUMMARY_CHOICES,
                        help='print a summary of the model, and exit - options: ' +
                        ' | '.join(SUMMARY_CHOICES))
    parser.add_argument('--compress', dest='compress', type=str, nargs='?', action='store',
                        help='configuration file for pruning the model (default is to use hard-coded schedule)')
    parser.add_argument('--sense', dest='sensitivity', choices=['element', 'filter', 'channel'], type=lambda s: s.lower(),
                        help='test the sensitivity of layers to pruning')
    parser.add_argument('--sense-range', dest='sensitivity_range', type=float, nargs=3, default=[0.0, 0.95, 0.05],
                        help='an optional parameter for sensitivity testing providing the range of sparsities to test.\n'
                        'This is equivalent to creating sensitivities = np.arange(start, stop, step)')
    parser.add_argument('--extras', default=None, type=str,
                        help='file with extra configuration information')
    parser.add_argument('--deterministic', '--det', action='store_true',
                        help='Ensure deterministic execution for re-producible results.')

    device_group = parser.add_mutually_exclusive_group()
    device_group.add_argument('--gpus', metavar='DEV_ID', default=None,
                        help='Comma-separated list of GPU device IDs to be used (default: use all available devices)')
    device_group.add_argument('--use-cpu', action='store_true', default=False,
                        help='Force use of CPU only')

    parser.add_argument('--name', '-n', metavar='NAME', default=None, help='Experiment name')
    parser.add_argument('--out-dir', '-o', dest='output_dir', default='logs', help='Path to dump logs and checkpoints')
    parser.add_argument('--validation-split', '--vs', type=float_range(exc_max=True), default=0, metavar='FRACTION',
                        help='Portion of training dataset to set aside for validation')
    parser.add_argument('--effective-train-size', '--etrs', type=float_range(exc_min=True), default=1.,
                        help='Portion of training dataset to be used in each epoch. '
                             'NOTE: If --validation-split is set, then the value of this argument is applied '
                             'AFTER the train-validation split according to that argument')
    parser.add_argument('--effective-valid-size', '--evs', type=float_range(exc_min=True), default=1.,
                        help='Portion of validation dataset to be used in each epoch. '
                             'NOTE: If --validation-split is set, then the value of this argument is applied '
                             'AFTER the train-validation split according to that argument')
    parser.add_argument('--effective-test-size', '--etes', type=float_range(exc_min=True), default=1.,
                        help='Portion of test dataset to be used in each epoch')
    parser.add_argument('--confusion', dest='display_confusion', default=False, action='store_true',
                        help='Display the confusion matrix')
    parser.add_argument('--earlyexit_lossweights', type=float, nargs='*', dest='earlyexit_lossweights', default=None,
                        help='List of loss weights for early exits (e.g. --earlyexit_lossweights 0.1 0.3)')
    parser.add_argument('--earlyexit_thresholds', type=float, nargs='*', dest='earlyexit_thresholds', default=None,
                        help='List of EarlyExit thresholds (e.g. --earlyexit_thresholds 1.2 0.9)')
    parser.add_argument('--num-best-scores', dest='num_best_scores', default=1, type=int,
                        help='number of best scores to track and report (default: 1)')
    parser.add_argument('--load-serialized', dest='load_serialized', action='store_true', default=False,
                        help='Load a model without DataParallel wrapping it')
    parser.add_argument('--thinnify', dest='thinnify', action='store_true', default=False,
                        help='physically remove zero-filters and create a smaller model')

    # deprecations
    def deprecation_warning(*args, old_keys=None, new_keys=None, **kwargs):
        if old_keys and new_keys:
            msglogger.warning('{okey} have been deprecated. Try {nkey} instead.'.format(
                okey=old_keys, nkey=new_keys))
        elif old_keys:
            msglogger.warning('{okey} have been deprecated.'.format(okey=old_keys))
        else:
            msglogger.warning('Some arguments have been deprecated and ignored.')

    parser.add_argument('--valid-size', '--validation-size',
                        type=functools.partial(deprecation_warning,
                            old_keys=['--valid-size', '--validation-size'],
                            new_keys=['--validation-split', '--vs']),
                        help=argparse.SUPPRESS)
    parser.add_argument('--print-freq', '-p',
                        type=functools.partial(deprecation_warning,
                            old_keys=['--print-freq', '-p'],
                            new_keys=['--print-period']),
                        help=argparse.SUPPRESS)
    parser.add_argument('-j', '--workers',
                        type=functools.partial(deprecation_warning,
                            old_keys=['-j', '--workers'],
                            new_keys=['--loaders']),
                        help=argparse.SUPPRESS)


    distiller.knowledge_distillation.add_distillation_args(parser, models.ALL_MODEL_NAMES, True)
    distiller.quantization.add_post_train_quant_args(parser)
    distiller.pruning.greedy_filter_pruning.add_greedy_pruner_args(parser)
    adc.automl_args.add_automl_args(parser)
    return parser


def getPrintPeriod(namespace, samples_number, batch_size):
    """Compute the appropriate print period.

    If print_period is set explicitly, use it. Otherwise, use print_frequency
    to compute the print period.
    """
    print_period = namespace.print_period
    if print_period is None:
        prints_per_epoch = (namespace.print_frequency if namespace.print_frequency
            is not None else DEFAULT_PRINT_FREQUENCY)
        batches = math.ceil(samples_number / batch_size)
        with contextlib.suppress(ZeroDivisionError):
            print_period = batches // prints_per_epoch

    # enforce value of period >= 1
    if print_period < 1:
        if (namespace.print_period is None) and (namespace.print_frequency is None):
            # both related arguments are unset by the user
            print_period = 1
        else:
            raise ValueError('print_period argument must be greater or equal to 1')

    return print_period
