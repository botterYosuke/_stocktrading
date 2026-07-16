"""gen3 の系列 NN (GRU + 6 セル・マルチタスクヘッド)。torch 依存はこのモジュールに閉じる。

- 学習は FIT_RANGE のみ、early stopping は ESTOP_RANGE の loss (公式 val 不使用)
- seed 固定 + cudnn.deterministic。GPU の GRU はビット単位の再現までは保証しない
  (seed・config hash・学習ログを成果物に残すことで追試可能性を担保する)
"""
from __future__ import annotations

import numpy as np
import torch
from torch import nn

from scalp_agent_bars.minute.nn_config import MODEL, SEQ_CHANNELS, STATIC_FEATURES, TRAIN


class Gen3Net(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.gru = nn.GRU(
            input_size=len(SEQ_CHANNELS),
            hidden_size=MODEL["hidden"],
            num_layers=MODEL["layers"],
            batch_first=True,
            dropout=MODEL["dropout"],
        )
        self.mlp = nn.Sequential(
            nn.Linear(MODEL["hidden"] + len(STATIC_FEATURES), MODEL["mlp_hidden"]),
            nn.ReLU(),
            nn.Dropout(MODEL["dropout"]),
        )
        self.heads = nn.ModuleList(
            [nn.Linear(MODEL["mlp_hidden"], MODEL["classes"]) for _ in range(MODEL["heads"])]
        )

    def forward(self, seq: torch.Tensor, sta: torch.Tensor) -> torch.Tensor:
        out, _ = self.gru(seq)
        z = self.mlp(torch.cat([out[:, -1, :], sta], dim=1))
        return torch.stack([h(z) for h in self.heads], dim=1)  # (B, heads, classes)


def _masked_loss(logits: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """y: (B, heads) int64、無効ラベル = -1。有効ヘッドの CE 平均。"""
    losses = []
    for hi in range(logits.shape[1]):
        t = y[:, hi]
        if (t >= 0).any():
            losses.append(nn.functional.cross_entropy(logits[:, hi, :], t, ignore_index=-1))
    return torch.stack(losses).mean()


def _eval_loss(model, seq, sta, y, idx, device, batch) -> float:
    model.eval()
    total, count = 0.0, 0
    with torch.no_grad():
        for lo in range(0, len(idx), batch):
            b = idx[lo:lo + batch]
            logits = model(
                torch.from_numpy(np.ascontiguousarray(seq[b])).to(device),
                torch.from_numpy(np.ascontiguousarray(sta[b])).to(device),
            )
            loss = _masked_loss(logits, torch.from_numpy(y[b].astype(np.int64)).to(device))
            total += float(loss) * len(b)
            count += len(b)
    return total / max(count, 1)


def train_model(
    seq: np.ndarray,
    sta: np.ndarray,
    y: np.ndarray,
    fit_idx: np.ndarray,
    estop_idx: np.ndarray,
    device: str,
    log=print,
) -> tuple[dict, list[dict]]:
    """学習して (best state_dict (CPU), 履歴) を返す。"""
    torch.manual_seed(TRAIN["seed"])
    torch.backends.cudnn.deterministic = True
    rng = np.random.default_rng(TRAIN["seed"])
    model = Gen3Net().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=TRAIN["lr"])
    batch = TRAIN["batch_size"]
    best_loss, best_state, bad = np.inf, None, 0
    history: list[dict] = []
    for epoch in range(TRAIN["max_epochs"]):
        model.train()
        order = rng.permutation(fit_idx)
        total, count = 0.0, 0
        for lo in range(0, len(order), batch):
            b = np.sort(order[lo:lo + batch])  # memmap は昇順アクセスが速い
            logits = model(
                torch.from_numpy(np.ascontiguousarray(seq[b])).to(device),
                torch.from_numpy(np.ascontiguousarray(sta[b])).to(device),
            )
            loss = _masked_loss(logits, torch.from_numpy(y[b].astype(np.int64)).to(device))
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += float(loss) * len(b)
            count += len(b)
        estop = _eval_loss(model, seq, sta, y, estop_idx, device, batch)
        history.append({"epoch": epoch, "fit_loss": total / max(count, 1), "estop_loss": estop})
        log(f"  epoch {epoch}: fit={total / max(count, 1):.5f} estop={estop:.5f}", flush=True)
        if estop < best_loss - 1e-5:
            best_loss, bad = estop, 0
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= TRAIN["estop_patience"]:
                log(f"  early stop at epoch {epoch} (best estop={best_loss:.5f})", flush=True)
                break
    assert best_state is not None
    return best_state, history


def predict_scores(
    state: dict, seq: np.ndarray, sta: np.ndarray, idx: np.ndarray, device: str
) -> np.ndarray:
    """(len(idx), heads, 3) の softmax score。未較正 score として扱う。"""
    model = Gen3Net().to(device)
    model.load_state_dict(state)
    model.eval()
    batch = TRAIN["batch_size"]
    out = np.empty((len(idx), MODEL["heads"], MODEL["classes"]), dtype=np.float32)
    with torch.no_grad():
        for lo in range(0, len(idx), batch):
            b = idx[lo:lo + batch]
            logits = model(
                torch.from_numpy(np.ascontiguousarray(seq[b])).to(device),
                torch.from_numpy(np.ascontiguousarray(sta[b])).to(device),
            )
            out[lo:lo + batch] = torch.softmax(logits, dim=2).cpu().numpy()
    return out
