"""Convert the GrooveClean detector weights (PyTorch checkpoint) into the plain NumPy file the second opinion
runs from without PyTorch: BatchNorm layers are folded into the convolutions they follow (inference only).

    python tools/convert_grooveclean.py vinylcheck/weights/grooveclean-detector.pt vinylcheck/weights/grooveclean-detector.npz

Needs PyTorch once, at conversion time. The source checkpoint stays in the repository for the optional PyTorch
backend; second_opinion.py checks both against each other in the tests.
"""

import sys

import numpy as np
import torch

EPS = 1e-5   # nn.BatchNorm1d default


def fold(weight, bias, bn_weight, bn_bias, mean, var):
    s = bn_weight / torch.sqrt(var + EPS)
    return weight * s[:, None, None], (bias - mean) * s + bn_bias


def main(src: str, dst: str) -> None:
    blob = torch.load(src, map_location="cpu", weights_only=True)
    cfg, sd = blob["config"], blob["state_dict"]
    out = {"channels": np.int64(cfg["channels"]), "blocks": np.int64(cfg["blocks"]), "kernel": np.int64(cfg["kernel"]),
           "stem_w": sd["stem.weight"], "stem_b": sd["stem.bias"]}
    for i in range(cfg["blocks"]):
        p = f"blocks.{i}."
        w, b = fold(sd[p + "conv.weight"], sd[p + "conv.bias"], sd[p + "norm.weight"], sd[p + "norm.bias"],
                    sd[p + "norm.running_mean"], sd[p + "norm.running_var"])
        out[f"block{i}_conv_w"], out[f"block{i}_conv_b"] = w, b
        w, b = fold(sd[p + "mix.weight"], sd[p + "mix.bias"], sd[p + "norm2.weight"], sd[p + "norm2.bias"],
                    sd[p + "norm2.running_mean"], sd[p + "norm2.running_var"])
        out[f"block{i}_mix_w"], out[f"block{i}_mix_b"] = w[:, :, 0], b
    out["head0_w"], out["head0_b"] = sd["head.0.weight"][:, :, 0], sd["head.0.bias"]
    out["head1_w"], out["head1_b"] = sd["head.2.weight"][:, :, 0], sd["head.2.bias"]
    arrays = {k: (v.numpy().astype(np.float32) if torch.is_tensor(v) else v) for k, v in out.items()}
    np.savez(dst, **arrays)
    print(f"{dst}: {len(arrays)} arrays")


if __name__ == "__main__":
    main(*sys.argv[1:3])
