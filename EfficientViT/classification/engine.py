"""
Train, eval, teacher-load functions used in main.py
"""
import math
import sys
from typing import Iterable, Optional

import torch

from timm.data import Mixup
from timm.utils import ModelEma

from losses import DistillationLoss
import utils

import torchvision.models as models


def load_custom_teacher_model(teacher_path):
    teacher_model = models.densenet121(pretrained=False, num_classes=14)
    
    checkpoint = torch.load(teacher_path, map_location="cpu")

    if "model" in checkpoint:
        checkpoint = checkpoint["model"]

    state_dict = {k: v for k, v in checkpoint.items() if k in teacher_model.state_dict()}
    
    missing_keys, unexpected_keys = teacher_model.load_state_dict(state_dict, strict=False)

    if missing_keys:
        print(f"Missing keys: {missing_keys}")
    if unexpected_keys:
        print(f"Unexpected keys: {unexpected_keys}")

    return teacher_model

def set_bn_state(model):
    for m in model.modules():
        if isinstance(m, torch.nn.modules.batchnorm._BatchNorm):
            m.eval()

def train_one_epoch(model: torch.nn.Module, criterion: DistillationLoss,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, loss_scaler,
                    clip_grad: float = 0,
                    clip_mode: str = 'norm',
                    model_ema: Optional[ModelEma] = None, mixup_fn: Optional[Mixup] = None,
                    set_training_mode=True,
                    set_bn_eval=False,):
    model.train(set_training_mode)
    if set_bn_eval:
        set_bn_state(model)
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(
        window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 100

    for samples, targets in metric_logger.log_every(
            data_loader, print_freq, header):
        samples = samples.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)

        if mixup_fn is not None:
            samples, targets = mixup_fn(samples, targets)

        if True:  # with torch.cuda.amp.autocast():
            outputs = model(samples)
            loss = criterion(samples, outputs, targets)

        loss_value = loss.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            sys.exit(1)

        optimizer.zero_grad()

        is_second_order = getattr(optimizer, 'is_second_order', False)

        loss_scaler(loss, optimizer, clip_grad=clip_grad, 
                    clip_mode=clip_mode,parameters=model.parameters(),
                    create_graph=is_second_order)



        torch.cuda.synchronize()
        if model_ema is not None:
            model_ema.update(model)

        metric_logger.update(loss=loss_value)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluate(data_loader, model, device):
    criterion = torch.nn.BCEWithLogitsLoss()

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    # switch to evaluation mode
    model.eval()

    for images, target in metric_logger.log_every(data_loader, 10, header):
        images = images.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)

        with torch.cuda.amp.autocast():
            output = model(images)
            loss = criterion(output, target)

        preds = torch.sigmoid(output) > 0.5
        correct = preds.eq(target).sum().item()
        total = target.numel()
        accuracy = correct / total

        batch_size = images.shape[0]
        metric_logger.update(loss=loss.item())
        metric_logger.meters['accuracy'].update(accuracy, n=batch_size)

    metric_logger.synchronize_between_processes()
    print('* Accuracy: {accuracy.global_avg:.3f} loss: {losses.global_avg:.3f}'
          .format(accuracy=metric_logger.accuracy, losses=metric_logger.loss))

    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}