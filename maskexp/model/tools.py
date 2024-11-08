import torch
import torch.nn as nn
import torch.nn.functional as F
import functools
import os
import json
from inspect import signature
from maskexp.definitions import IGNORE_LABEL_INDEX, DEFAULT_MASK_EVENT, VELOCITY_MASK_EVENT
from pathlib import Path
from maskexp.magenta.models.performance_rnn import performance_model

MAX_SEQ_LEN = 128


class ExpConfig:
    def __init__(self, model_name='', save_dir='', data_path='', perf_config_name='',
                 n_embed=256, n_layers=4, n_heads=4, dropout=0.1, max_seq_len=MAX_SEQ_LEN, special_tokens=None,
                 device=torch.device('mps'), n_epochs=20, mlm_prob=0.15, lr=1e-4, eval_interval=5, save_interval=5,
                 resume_from=None, train_loss=None, val_loss=None, total_epoch_num=0):
        psavedir = Path(save_dir)
        if not psavedir.exists():
            psavedir.mkdir(parents=True)
        assert os.path.exists(data_path)
        ckpt_save_path = os.path.join(save_dir, model_name, '.pth')
        if os.path.exists(ckpt_save_path):
            raise FileExistsError(f"found existing checkpoint file at {ckpt_save_path}")
        if perf_config_name not in performance_model.default_configs.keys():
            raise KeyError(f"Performance config key: {perf_config_name} not found")
        if special_tokens is None:
            print("\x1B[33m[Warning]\033[0m Special token(s) is required for MLM training")

        # IO Paths
        self.model_name = model_name  # Will be used to name the saved file
        self.save_dir = save_dir  # two folders will be created - checkpoints, logs
        self.data_path = data_path
        self.perf_config_name = perf_config_name

        # Model Setting
        self.n_embed = n_embed
        self.max_seq_len = max_seq_len
        self.n_layers = n_layers
        self.n_heads = n_heads
        self.dropout = dropout

        # Training Setting
        self.lr = lr
        self.mlm_prob = mlm_prob
        self.device = device
        self.n_epochs = n_epochs
        self.eval_intv = eval_interval
        self.save_intv = save_interval
        self.special_tokens = special_tokens

        self.resume_from = resume_from  # Provide checkpoint path to resume

        self.train_loss = [] if train_loss is None else train_loss
        self.val_loss = [] if val_loss is None else val_loss
        self.total_epoch_num = total_epoch_num

    @classmethod
    def load_from_dict(cls, json_cfg):
        init_params = signature(cls.__init__).parameters
        filtered_cfg = {key: value for key, value in json_cfg.items() if key in init_params}
        return cls(**filtered_cfg)

    def serialize(self):
        params = signature(self.__init__).parameters
        data = {param: getattr(self, param) for param in params if
                hasattr(self, param) and isinstance(getattr(self, param), (int, str, float))}
        return json.dumps(data, indent=4)


def print_model(ckpt_path):
    pth = torch.load(ckpt_path)
    cfg = ExpConfig.load_from_dict(pth)
    print(cfg.serialize())


def save_checkpoint(model, optimizer, num_epoch, train_loss, val_loss, cfg: ExpConfig = None, save_dir='checkpoint',
                    name='checkpoint'):
    path = Path(save_dir)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=False)

    all_train_loss = cfg.train_loss.copy()
    all_train_loss.extend(train_loss)

    all_val_loss = cfg.val_loss.copy()
    all_val_loss.extend(val_loss)

    checkpoint = {
        'total_epoch_num': cfg.total_epoch_num + num_epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'train_loss': all_train_loss,
        'val_loss': all_val_loss
    }
    if cfg is None:
        print("\x1B[33m[Warning]\033[0m model setting is not provided...recording only the state dict and losses")
    else:
        checkpoint = cfg.__dict__ | checkpoint

    torch.save(checkpoint, f'{save_dir}/{name}.pth')
    print(f"\x1B[34m[Info]\033[0m Checkpoint saved to {save_dir}/{name}.pth")


def load_torch_model(path):
    return torch.load(path, map_location=torch.device('cpu'))


def load_model(model, optimizer=None, cpath=''):
    ckpt_path = Path(cpath)
    if not ckpt_path.exists():
        raise FileNotFoundError(f'Checkpoint file not found: {ckpt_path}')
    ckpt = load_torch_model(path=cpath)
    load_model_from_pth(model, optimizer, ckpt)


def load_model_from_pth(model, optimizer=None, pth=None):
    if pth is None:
        raise ValueError(".pth file must be provided")
    model.load_state_dict(pth['model_state_dict'])
    if optimizer is not None:
        optimizer.load_state_dict(pth['optimizer_state_dict'])


def decode_batch_perf_logits(logits, decoder=None, idx=None):
    if decoder is None:
        raise ValueError("Decoder is required")
    if idx is None:
        pred_ids = torch.argmax(F.softmax(logits, dim=-1), dim=-1).tolist()
    else:
        pred_ids = torch.argmax(F.softmax(logits, dim=-1), dim=-1)[idx, :].tolist()
    out = []
    for tk in pred_ids:
        out.append(decoder.decode_event(tk))
    return out


def logits_to_id(logits):
    pred_ids = torch.argmax(F.softmax(logits, dim=-1), dim=-1).tolist()
    return torch.tensor(pred_ids[0])


def decode_perf_logits(logits, decoder=None):
    if decoder is None:
        raise ValueError("Decoder is required")
    pred_ids = torch.argmax(F.softmax(logits, dim=-1), dim=-1).tolist()
    out = []
    for tk in pred_ids[0]:
        out.append(decoder.decode_event(tk))
    return out


def compare_perf_event(e, target_event):
    if e.event_value == target_event.event_value and e.event_type == target_event.event_type:
        return True
    return False


def print_perf_seq(perf_seq):
    outstr = ''
    for e in perf_seq:
        if compare_perf_event(e, DEFAULT_MASK_EVENT):
            outstr += f'\x1B[34m[{e.event_type}-{e.event_value}],\033[0m\t'
        elif compare_perf_event(e, VELOCITY_MASK_EVENT):
            outstr += f'\x1B[33m[{e.event_type}-{e.event_value}],\033[0m\t'
        else:
            outstr += f'[{e.event_type}-{e.event_value}],\t'
    print(outstr)


def cross_entropy_loss(logits, labels, masks=None, consider_mask=None):
    if masks is None:
        raise ValueError("Masks are needed to evaluate MLM, else all tokens will be considered!")
    if consider_mask is None:
        valid_mask = torch.greater(masks, 0)
    else:
        assert isinstance(consider_mask, torch.Tensor)
        valid_mask = torch.isin(masks, consider_mask)

    valid_logits = logits[valid_mask]
    valid_labels = labels[valid_mask]
    if valid_logits.numel() == 0:
        return 0  # No valid elements to calculate loss
    loss_fn = nn.CrossEntropyLoss()
    return loss_fn(valid_logits.view(-1, valid_logits.size(-1)), valid_labels.view(-1)).item()


def hits_at_k(logits, labels, k=3, masks=None, consider_mask=None):
    """
    HITS@K metric
    :param logits:
    :param labels:
    :param k:
    :param masks:
    :param consider_mask:
    :return:
    """
    if masks is None:
        raise ValueError("Masks are needed to evaluate MLM, else all tokens will be considered!")
    if consider_mask is None:
        valid_mask = torch.greater(masks, 0)
    else:
        assert isinstance(consider_mask, torch.Tensor)
        valid_mask = torch.isin(masks, consider_mask)

    valid_logits = logits[valid_mask]
    valid_labels = labels[valid_mask]

    if valid_labels.numel() == 0:
        return float('nan')

    _, valid_topk_indices = torch.topk(valid_logits, k, dim=-1)
    # .mean() effectively calculates the binary hit/miss values
    hits = (valid_topk_indices == valid_labels.unsqueeze(-1)).any(dim=-1).float().mean().item()
    return hits


def accuracy_within_n(logits, labels, n=1, masks=None, consider_mask=None):
    """
    Calculate accuracy ± n for ordinal classification
    :param logits:
    :param labels:
    :param n:
    :param masks:
    :param consider_mask:
    :return:
    """
    if masks is None:
        raise ValueError("Masks are needed to evaluate MLM, else all tokens will be considered!")
    if consider_mask is None:
        valid_mask = torch.greater(masks, 0)
    else:
        assert isinstance(consider_mask, torch.Tensor)
        valid_mask = torch.isin(masks, consider_mask)

    valid_logits = logits[valid_mask]
    valid_labels = labels[valid_mask]

    if valid_labels.numel() == 0:
        return float('nan')
    valid_pred = valid_logits.argmax(dim=-1)
    acc = (torch.abs(valid_pred - valid_labels) <= n).float().mean().item()
    return acc


def bind_metric(func, **kwargs):
    partial_func = functools.partial(func, **kwargs)
    arg_str = "_".join(f"{key}{value}" for key, value in kwargs.items())
    partial_func.__name__ = f"{func.__name__}_{arg_str}"
    return partial_func


if __name__ == '__main__':
    print_model('/Users/kurono/Documents/python/GEC/ExpressiveMLM/save/checkpoints/velocitymlm.pth')
