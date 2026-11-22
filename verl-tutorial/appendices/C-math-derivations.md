# 附录 C 数学推导

## C.1 策略梯度

目标：

$$
J(\theta)=\mathbb{E}_{\tau\sim\pi_\theta}[R(\tau)]
=\int \pi_\theta(\tau)R(\tau)\,d\tau.
$$

求导：

$$
\nabla_\theta J
=\int \nabla_\theta\pi_\theta(\tau)R(\tau)\,d\tau
=\int \pi_\theta(\tau)\nabla_\theta\log\pi_\theta(\tau)R(\tau)\,d\tau
=\mathbb{E}\left[R(\tau)\nabla_\theta\log\pi_\theta(\tau)\right].
$$

轨迹概率分解：

$$
\pi_\theta(\tau)=p(s_0)\prod_{t=0}^{T-1}\pi_\theta(a_t\mid s_t)p(s_{t+1}\mid s_t,a_t).
$$

取对数后，只有 $\log\pi_\theta(a_t\mid s_t)$ 含 $\theta$，所以：

$$
\nabla_\theta J
=\mathbb{E}\left[\sum_t R(\tau)\nabla_\theta\log\pi_\theta(a_t\mid s_t)\right].
$$

## C.2 baseline 不改变期望

对任意只依赖 $s_t$ 的 $b(s_t)$：

$$
\mathbb{E}_{a_t\sim\pi_\theta}\left[b(s_t)\nabla_\theta\log\pi_\theta(a_t\mid s_t)\right]
=b(s_t)\sum_{a}\nabla_\theta\pi_\theta(a\mid s_t)
=b(s_t)\nabla_\theta 1=0.
$$

所以减去 baseline 不改变梯度期望，只降低方差。

## C.3 GAE 推导

定义：

$$
\delta_t=r_t+\gamma V(s_{t+1})-V(s_t).
$$

$n$ 步优势：

$$
A_t^{(n)}=\sum_{l=0}^{n-1}\gamma^l\delta_{t+l}.
$$

验证 $n=1$：

$$
A_t^{(1)}=r_t+\gamma V(s_{t+1})-V(s_t)=Q(s_t,a_t)-V(s_t).
$$

GAE 对 $n$ 做指数加权：

$$
A_t^{\text{GAE}}=\sum_{n=1}^{\infty}(1-\lambda)\lambda^{n-1}A_t^{(n)}.
$$

展开可得：

$$
A_t^{\text{GAE}}=\sum_{l=0}^{\infty}(\gamma\lambda)^l\delta_{t+l}.
$$

递归形式：

$$
A_t=\delta_t+\gamma\lambda A_{t+1}.
$$

## C.4 PPO 的 clip

未裁剪目标：

$$
L^{\text{PG}}=\mathbb{E}\left[\rho_t A_t\right],\qquad
\rho_t=\frac{\pi_\theta}{\pi_{\text{old}}}.
$$

裁剪目标：

$$
L^{\text{CLIP}}=\mathbb{E}\left[\min\left(\rho_t A_t,\operatorname{clip}(\rho_t,1-\epsilon,1+\epsilon)A_t\right)\right].
$$

当 $A_t>0$ 且 $\rho_t>1+\epsilon$ 时，梯度被截断；当 $A_t<0$ 且 $\rho_t<1-\epsilon$ 时，梯度也被截断。它防止一次更新把策略推得太远。

## C.5 KL 估计量

真实 KL：

$$
\mathrm{KL}(p\Vert q)=\mathbb{E}_{p}\left[\log\frac{p}{q}\right].
$$

k1：

$$
\log p-\log q.
$$

k2：

$$
\frac12(\log p-\log q)^2.
$$

k3：

$$
e^{\log q-\log p}-(\log q-\log p)-1.
$$

令 $r=q/p$，k3 为 $r-\log r-1$，在 $r=1$ 附近是二阶小量，方差低。

## C.6 GRPO

对每组 $G$ 条样本：

$$
\mu=\frac1G\sum_i r_i,\qquad
\sigma=\sqrt{\frac1{G-1}\sum_i(r_i-\mu)^2}.
$$

优势：

$$
A_i=\frac{r_i-\mu}{\sigma+\epsilon}.
$$

性质：

- 组内均值为 0；
- 组内标准差为 1（近似）；
- 不需要 critic；
- 组内奖励全相同 -> 优势全 0。

## C.7 GSPO

序列级比率：

$$
\rho_i=\exp\left(\frac{1}{|y_i|}\sum_{t\in y_i}\log\frac{\pi_\theta(y_t\mid s_t)}{\pi_{\text{old}}(y_t\mid s_t)}\right).
$$

它是 token 比率的几何平均。对整条序列使用同一个 $\rho_i$，降低方差。

## C.8 DPO

Bradley-Terry 偏好模型：

$$
P(y_w\succ y_l\mid x)=\sigma\left(\beta\log\frac{\pi_\theta(y_w\mid x)}{\pi_{\text{ref}}(y_w\mid x)}-\beta\log\frac{\pi_\theta(y_l\mid x)}{\pi_{\text{ref}}(y_l\mid x)}\right).
$$

负对数似然：

$$
\mathcal{L}_{\text{DPO}}=-\log\sigma\left(\beta\left[\log\frac{\pi_\theta(y_w)}{\pi_{\text{ref}}(y_w)}-\log\frac{\pi_\theta(y_l)}{\pi_{\text{ref}}(y_l)}\right]\right).
$$

## C.9 值损失

$$
\mathcal{L}_V=\frac12\mathbb{E}\left[\max\left((V_\phi-\hat R)^2,(\operatorname{clip}(V_\phi,V_{\text{old}}-\epsilon,V_{\text{old}}+\epsilon)-\hat R)^2\right)\right].
$$

裁剪防止 critic 一次更新太远。

## C.10 loss aggregation

token-mean：

$$
\mathcal{L}=\frac{\sum_{b,t}m_{b,t}\ell_{b,t}}{\sum_{b,t}m_{b,t}}.
$$

token-sum：

$$
\mathcal{L}=\sum_{b,t}m_{b,t}\ell_{b,t}.
$$

它不按 token 数归一，loss 尺度随 batch/长度变化；只有当你显式想要“全局 token 总损失”时才用。

seq-mean-token-sum：

$$
\mathcal{L}=\frac{1}{B}\sum_b\sum_t m_{b,t}\ell_{b,t}.
$$

seq-mean-token-sum-norm：

$$
\mathcal{L}=\frac{1}{B\cdot H}\sum_b\sum_t m_{b,t}\ell_{b,t},
$$

其中 $H$ 是固定 horizon（通常是最大响应长度）。

seq-mean-token-mean：

$$
\mathcal{L}=\frac{1}{B}\sum_b\frac{\sum_t m_{b,t}\ell_{b,t}}{\sum_t m_{b,t}}.
$$

这些模式的差别决定了长度偏置。

## C.11 重要性采样

行为策略 $\mu$ 采样，目标策略 $\pi$：

$$
\mathbb{E}_{x\sim\pi}[f(x)]=\mathbb{E}_{x\sim\mu}\left[\frac{\pi(x)}{\mu(x)}f(x)\right].
$$

方差随比率增大而爆炸，所以实践中截断：

$$
\tilde w=\operatorname{clip}\left(\frac{\pi}{\mu},\frac1c,c\right).
$$

异步 RL 的 rollout correction 就是这个公式。

## C.12 GDPO：分维度归一化

设每个样本有 $K$ 个奖励维度 $r_{i,k}$。GRPO 先把它们加权求和再组内标准化；GDPO 先对每个维度分别标准化：

$$
A_{i,k}=\frac{r_{i,k}-\mu_{g,k}}{\sigma_{g,k}+\epsilon},
\qquad
A_i=\sum_{k=1}^{K}w_k A_{i,k},
$$

最后在 batch 范围内做 masked whitening：

$$
\tilde A_i=\frac{A_i-\mu_{\text{batch}}}{\sigma_{\text{batch}}+\epsilon}.
$$

如果某个维度的尺度是另一个的 100 倍，先求和会让小尺度维度几乎不贡献梯度；分维度标准化后，$w_k$ 才是显式可调的相对权重。代价是失去“各维度原始量纲”的含义，且要求 reward 函数返回分维度字典。

## C.13 OPO：长度加权基线

OPO 用长度加权的组内平均作为基线：

$$
b_g=\frac{\sum_{i\in g}L_i r_i}{\sum_{i\in g}L_i},
\qquad
A_i=r_i-b_g,
$$

其中 $L_i$ 是第 $i$ 条响应的有效 token 数。它不做 std 归一化。

把基线看成加权最小二乘的解：最小化

$$
\sum_{i\in g}L_i(r_i-b)^2
$$

对 $b$ 求导并令零，得到 $b=\sum_i L_i r_i/\sum_i L_i$。所以 OPO 的基线是“以长度为权重的最优常数基线”；长度越大的轨迹对基线的估计影响越大，避免了短轨迹（可能只是提前 EOS）把基线拉偏。

## C.14 Pass@k 优势

`grpo_passk` 只给组内最高分样本非零优势：

$$
i^*=\arg\max_i r_i,\qquad
A_{i^*}=r_{i^*}-r_{(2)},\qquad
A_i=0\ (i\ne i^*),
$$

其中 $r_{(2)}$ 是组内第二高的奖励。若 `norm_adv_by_std_in_grpo=true`，再除以组内标准差。

它对应 pass@k 的目标：一组里只要有一条成功，整组就算成功；因此梯度只强化“最好的那条”，而不是把整组往均值拉。代价是有效样本数从 $G$ 降到 1，方差更大；通常要增大 batch 或降低学习率。

## C.15 OTB：token 级最优基线

OTB 对每个 token 位置 $t$ 估计一个基线：

$$
B_t^*=\frac{\sum_i G_t^{(i)}W_t^{(i)}}{\sum_i W_t^{(i)}+\epsilon},
\qquad
W_t=\sum_{j=1}^{t}\left(1-2\pi_j+\sum_v\pi_{j,v}^2\right),
$$

其中 $G_t$ 是 reward-to-go，$W_t$ 是累积“实现能量”（realized energy）。最终优势：

$$
A_t=(G_t-B_t^*)\,m_t.
$$

直觉：$1-2\pi_j+\sum_v\pi_{j,v}^2$ 是策略在 token $j$ 上的不确定性代理（logit 梯度范数的期望）；累积 $W_t$ 越大的位置，说明前面的决策越“高方差”，基线的估计应该给它更高权重。$B_t^*$ 是加权最小二乘

$$
\min_b\sum_i W_t^{(i)}(G_t^{(i)}-b)^2
$$

的解，因此它是该 token 上的最优常数基线。OTB 需要 $\sum_v\pi_{j,v}^2$（`calculate_sum_pi_squared=true`），并且不能用 fused log-prob kernel（它不吐 logits）。
