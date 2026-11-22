import torch
import torch.nn.functional as F

from mini_megatron.parallel.cross_entropy import vocab_parallel_cross_entropy
from mini_megatron.parallel.state import initialize_model_parallel


def test_matches_torch_with_ignore_index():
    initialize_model_parallel(1, 1, 1)
    logits = torch.randn(8, 32, requires_grad=True)
    labels = torch.randint(0, 32, (8,))
    labels[1] = -100
    labels[5] = -100
    loss = vocab_parallel_cross_entropy(logits, labels)
    ref = F.cross_entropy(logits, labels, ignore_index=-100)
    assert torch.allclose(loss, ref, atol=1e-5)


def test_gradient_matches_torch():
    initialize_model_parallel(1, 1, 1)
    logits = torch.randn(6, 16, requires_grad=True)
    labels = torch.tensor([0, 3, 7, 15, -100, 2])
    loss = vocab_parallel_cross_entropy(logits, labels)
    loss.backward()
    grad = logits.grad.clone()

    logits2 = logits.detach().clone().requires_grad_(True)
    ref = F.cross_entropy(logits2, labels, ignore_index=-100)
    ref.backward()
    assert torch.allclose(grad, logits2.grad, atol=1e-6)


def test_all_ignored_is_zero():
    initialize_model_parallel(1, 1, 1)
    logits = torch.randn(4, 8, requires_grad=True)
    labels = torch.full((4,), -100)
    loss = vocab_parallel_cross_entropy(logits, labels)
    assert loss.item() == 0.0
    loss.backward()
    assert torch.count_nonzero(logits.grad) == 0


def test_bf16_logits_compute_in_fp32_and_return_matching_grad_dtype():
    initialize_model_parallel(1, 1, 1)
    torch.manual_seed(7)
    logits32 = torch.randn(4, 16)
    labels = torch.tensor([0, 3, 7, 15])
    logits = logits32.to(torch.bfloat16).requires_grad_(True)
    loss = vocab_parallel_cross_entropy(logits, labels)
    loss.backward()
    # The forward math is fp32; the loss is returned in the logits dtype and
    # the gradient must match the input dtype for autograd.
    assert loss.dtype == torch.bfloat16
    assert logits.grad.dtype == torch.bfloat16
    ref = F.cross_entropy(logits32, labels)
    assert torch.allclose(loss.float(), ref, atol=5e-2, rtol=5e-2)


def test_z_loss_is_finite():
    initialize_model_parallel(1, 1, 1)
    logits = torch.randn(4, 8, requires_grad=True)
    labels = torch.tensor([0, 1, 2, 3])
    loss = vocab_parallel_cross_entropy(logits, labels, z_loss_coeff=0.001)
    loss.backward()
    assert torch.isfinite(loss)
    assert torch.isfinite(logits.grad).all()
