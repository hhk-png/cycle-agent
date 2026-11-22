import copy

import torch

from mini_megatron.config import ExperimentConfig
from mini_megatron.parallel.state import initialize_model_parallel
from mini_megatron.parallel.transformer import TransformerStage


def make_config(**model_kwargs):
    cfg = ExperimentConfig.from_json("configs/tiny.json")
    for key, value in model_kwargs.items():
        setattr(cfg.model, key, value)
    return cfg


def test_forward_backward_and_gqa():
    initialize_model_parallel(1, 1, 1)
    cfg = make_config(num_query_groups=2, max_seq_len=32)
    stage = TransformerStage(cfg.model, cfg.parallel)
    ids = torch.randint(0, 128, (2, 16))
    labels = torch.randint(0, 128, (2, 16))
    out = stage(input_ids=ids, labels=labels)
    assert out.logits.shape == (2, 16, 128)
    assert out.loss is not None and torch.isfinite(out.loss)
    out.loss.backward()
    assert any(p.grad is not None for p in stage.parameters())


def test_learned_position_embedding():
    initialize_model_parallel(1, 1, 1)
    cfg = make_config(position_embedding="learned")
    stage = TransformerStage(cfg.model, cfg.parallel)
    ids = torch.randint(0, 128, (2, 8))
    out = stage(input_ids=ids, labels=ids)
    assert torch.isfinite(out.loss)


def test_moe_aux_loss_and_backward():
    initialize_model_parallel(1, 1, 1)
    cfg = make_config(use_moe=True, num_experts=4, moe_topk=2)
    stage = TransformerStage(cfg.model, cfg.parallel)
    ids = torch.randint(0, 128, (2, 8))
    out = stage(input_ids=ids, labels=ids)
    assert out.aux_loss is not None and out.aux_loss.item() > 0
    (out.loss + out.aux_loss).backward()
    router_grad = stage.layers[0].mlp.router.weight.grad
    assert router_grad is not None and torch.isfinite(router_grad).all()


def test_tied_embeddings():
    initialize_model_parallel(1, 1, 1)
    cfg = make_config(tie_word_embeddings=True)
    stage = TransformerStage(cfg.model, cfg.parallel)
    assert stage.output_layer.weight is stage.embedding.weight


def test_activation_checkpointing():
    initialize_model_parallel(1, 1, 1)
    cfg = make_config(activation_checkpointing=True)
    stage = TransformerStage(cfg.model, cfg.parallel)
    ids = torch.randint(0, 128, (2, 8))
    out = stage(input_ids=ids, labels=ids)
    out.loss.backward()
    assert torch.isfinite(out.loss)


def test_generation_greedy():
    initialize_model_parallel(1, 1, 1)
    cfg = make_config()
    stage = TransformerStage(cfg.model, cfg.parallel)
    from mini_megatron.generation import generate

    prompt = torch.tensor([[1, 2, 3]])
    out = generate(stage, prompt, max_new_tokens=4, temperature=0)
    assert out.shape == (1, 7)
    assert out.min() >= 0 and out.max() < cfg.model.vocab_size


def test_fp8_simulation_forward_backward():
    initialize_model_parallel(1, 1, 1)
    cfg = make_config(fp8_simulation=True)
    stage = TransformerStage(cfg.model, cfg.parallel)
    ids = torch.randint(0, 128, (2, 8))
    out = stage(input_ids=ids, labels=ids)
    assert torch.isfinite(out.loss)
    out.loss.backward()
    grads = [p.grad for p in stage.parameters() if p.grad is not None]
    assert grads
    assert all(torch.isfinite(g).all() for g in grads)
    # A finite-only assertion would pass if every gradient were zero (the
    # original FP8 simulation bug).  At least one parameter must actually move.
    assert any(float(g.abs().sum()) > 0 for g in grads)


def test_autocast_training_step():
    from mini_megatron.training import Trainer

    initialize_model_parallel(1, 1, 1)
    cfg = make_config()
    cfg.train.max_steps = 1
    cfg.train.checkpoint_dir = ""
    cfg.train.use_autocast = True
    cfg.train.dtype = "bfloat16"
    trainer = Trainer(cfg)
    record = trainer.train_step(0)
    assert torch.isfinite(torch.tensor(record["loss"]))
