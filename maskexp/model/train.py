import pickle

from maskexp.model.create_dataset import load_dataset
from maskexp.definitions import DATA_DIR, OUTPUT_DIR
from pathlib import Path
from maskexp.util.prepare_data import mask_perf_tokens
from maskexp.model.tools import print_perf_seq, decode_perf_logits, MAX_SEQ_LEN, ExpConfig, load_ckpt
import time
import os
import torch
import tqdm
import note_seq
import matplotlib.pyplot as plt
from maskexp.model.bert import NanoBertMLM
from maskexp.magenta.models.performance_rnn import performance_model

NDEBUG = True


def save_checkpoint(model, optimizer, epoch, train_loss, val_loss, save_dir='checkpoint', name='checkpoint'):
    path = Path(save_dir)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=False)
    checkpoint = {
        'epoch': epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'train_loss': train_loss,
        'val_loss': val_loss
    }
    torch.save(checkpoint, f'{save_dir}/{name}.pth')
    print(f"\x1B[34m[Info]\033[0m Checkpoint saved to {save_dir}/{name}.pth")


def train_mlm(model, optimizer, train_dataloader, val_dataloader, cfg: ExpConfig = None):
    if cfg is None:
        raise ValueError("Model setting/config is required for training")
    train_loss, val_loss = [], []
    perf_config = performance_model.default_configs[cfg.perf_config_name]
    tokenizer = perf_config.encoder_decoder
    for epoch in range(cfg.n_epochs):
        train_start = time.time()
        model.train()
        total_loss = 0

        for step, batch in enumerate(tqdm.tqdm(train_dataloader)):
            optimizer.zero_grad()
            input_ids = batch[0]
            attention_mask = batch[1]

            # Mask tokens
            inputs, labels = mask_perf_tokens(input_ids, perf_config=perf_config, mask_prob=cfg.mlm_prob,
                                              special_ids=(note_seq.PerformanceEvent.VELOCITY,))
            inputs = inputs.to(cfg.device)
            attention_mask = attention_mask.to(cfg.device)
            labels = labels.to(cfg.device)

            loss, preds = model(inputs, attention_mask=attention_mask, labels=labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
            if not NDEBUG:
                break

        avg_train_loss = total_loss / len(train_dataloader)
        train_end = time.time()
        print(
            f'Epoch {epoch + 1}/{cfg.n_epochs}, Training Loss: {avg_train_loss}, Training Time: {train_end - train_start}')
        train_loss.append(avg_train_loss)

        erange = epoch + 1 if NDEBUG else epoch
        if erange % cfg.eval_intv == 0:
            model.eval()
            total_val_loss = 0
            with torch.no_grad():
                for i_b, batch in enumerate(val_dataloader):
                    input_ids = batch[0]
                    attention_mask = batch[1]

                    # Mask tokens
                    inputs, labels = mask_perf_tokens(input_ids, perf_config=perf_config, mask_prob=cfg.mlm_prob,
                                                      special_ids=(note_seq.PerformanceEvent.VELOCITY,))

                    inputs = inputs.to(cfg.device)
                    attention_mask = attention_mask.to(cfg.device)
                    labels = labels.to(cfg.device)

                    loss, logits = model(inputs, attention_mask=attention_mask, labels=labels)
                    total_val_loss += loss.item()

                    if i_b == len(val_dataloader) - 1:
                        decoded = [tokenizer.class_index_to_event(i, None) for i in inputs[0, :].tolist()]
                        print_perf_seq(decoded)
                        print_perf_seq(decode_perf_logits(logits, tokenizer._one_hot_encoding))

                    if not NDEBUG:
                        break

                avg_val_loss = total_val_loss / len(val_dataloader)
                print(f'Epoch {epoch + 1}/{cfg.n_epochs}, Validation Loss: {avg_val_loss}')
                val_loss.append(avg_val_loss)

                # if epoch % 5 == 0 or epoch == n_epochs - 1:
                save_checkpoint(model, optimizer, 0, train_loss, val_loss,
                                save_dir=f'{cfg.save_dir}/checkpoints',
                                name=cfg.model_name)

    return train_loss, val_loss


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def generate_loss_plot(train_loss, val_loss, eval_intv, save_dir, name):
    val_x = [(epoch + 1) * eval_intv for epoch in range(len(val_loss))]

    # Plot training and validation loss
    plt.plot(range(1, len(train_loss) + 1), train_loss, label='Train Loss')
    plt.plot(val_x, val_loss, label='Val Loss')

    plt.title('Training and Validation Losses')
    plt.xlabel('Epochs')
    plt.ylabel('Loss')
    plt.legend()

    path = Path(f'{save_dir}/logs')
    if not path.exists():
        path.mkdir(parents=True, exist_ok=False)
    plt.savefig(path / f'{name}_tr_loss.png')
    plt.show()


def run_mlm_train(cfg: ExpConfig = None):
    if cfg is None:
        raise ValueError("Settings must be provided for training")

    config = performance_model.default_configs[cfg.perf_config_name]

    train, val, _ = load_dataset(cfg.data_path)
    config = config

    model = NanoBertMLM(vocab_size=config.encoder_decoder.num_classes,
                        n_embed=cfg.n_embed,
                        max_seq_len=cfg.max_seq_len,
                        n_layers=cfg.n_layers,
                        n_heads=cfg.n_heads,
                        dropout=cfg.dropout)
    print('n params:', count_parameters(model))
    model.to(cfg.device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
    if cfg.resume_from is not None:
        print(f'\x1B[34m[Info]\033[0m Loading model from checkpoint: {cfg.resume_from}')
        load_ckpt(model, optimizer, cpath=cfg.resume_from)

    train_loss, val_loss = train_mlm(model, optimizer, train, val, cfg=cfg)
    generate_loss_plot(train_loss, val_loss, cfg.eval_intv, save_dir=cfg.save_dir, name=cfg.model_name)


def train_velocitymlm():
    cfg = ExpConfig(model_name='velocitymlm', save_dir='/Users/kurono/Documents/python/GEC/ExpressiveMLM/save',
                    data_path='/Users/kurono/Documents/python/GEC/ExpressiveMLM/data/mstro_with_dyn.pt',
                    perf_config_name='performance_with_dynamics',
                    special_tokens=(note_seq.PerformanceEvent.VELOCITY,),
                    n_embed=256, max_seq_len=MAX_SEQ_LEN, n_layers=4, n_heads=4, dropout=0.1,
                    device=torch.device('mps'), mlm_prob=0.15, n_epochs=20,
                    )
    run_mlm_train(cfg)


def continue_velocitymlm():
    cfg = ExpConfig(model_name='velocitymlm+', save_dir='/Users/kurono/Documents/python/GEC/ExpressiveMLM/save',
                    data_path='/Users/kurono/Documents/python/GEC/ExpressiveMLM/data/mstro_with_dyn.pt',
                    perf_config_name='performance_with_dynamics',
                    special_tokens=(note_seq.PerformanceEvent.VELOCITY,),
                    resume_from='/Users/kurono/Documents/python/GEC/ExpressiveMLM/save/checkpoints/velocitymlm.pth',
                    n_embed=256, max_seq_len=MAX_SEQ_LEN, n_layers=4, n_heads=4, dropout=0.1,
                    device=torch.device('mps'), mlm_prob=0.15, n_epochs=20,
                    )
    run_mlm_train(cfg)


if __name__ == '__main__':
    continue_velocitymlm()
    pass