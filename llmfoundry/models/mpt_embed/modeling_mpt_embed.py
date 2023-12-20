# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

"""A simple, flexible implementation of a GPT model.

Inspired by https://github.com/karpathy/minGPT/blob/master/mingpt/model.py
"""

import math
import warnings
from typing import (Any, Dict, List, Mapping, MutableMapping, Optional, Tuple,
                    Union)

import torch
import torch.nn as nn
import torch.nn.functional as F
from composer.metrics import (InContextLearningCodeEvalAccuracy,
                              InContextLearningLMAccuracy,
                              InContextLearningLMExpectedCalibrationError,
                              InContextLearningMCExpectedCalibrationError,
                              InContextLearningMultipleChoiceAccuracy,
                              InContextLearningQAAccuracy,
                              LossMetric) # JP Added
from composer.metrics.nlp import LanguageCrossEntropy, LanguagePerplexity
from composer.models import HuggingFaceModel
from composer.utils import dist
from omegaconf import DictConfig
from omegaconf import OmegaConf as om
from transformers import PreTrainedModel, PreTrainedTokenizerBase
from transformers.modeling_outputs import (BaseModelOutputWithPast,
                                           CausalLMOutputWithPast)

from llmfoundry.models.layers.attention import attn_bias_shape, build_attn_bias
from llmfoundry.models.layers.blocks import MPTBlock
from llmfoundry.models.layers.custom_embedding import SharedEmbedding
from llmfoundry.models.layers.fc import FC_CLASS_REGISTRY as FC_CLASS_REGISTRY
from llmfoundry.models.layers.ffn import \
    FFN_CLASS_REGISTRY as FFN_CLASS_REGISTRY
from llmfoundry.models.layers.ffn import MPTMLP as MPTMLP
from llmfoundry.models.layers.ffn import build_ffn as build_ffn
from llmfoundry.models.layers.norm import NORM_CLASS_REGISTRY
from llmfoundry.models.mpt.configuration_mpt import MPTConfig

# NOTE: All utils are imported directly even if unused so that
# HuggingFace can detect all the needed files to copy into its modules folder.
# Otherwise, certain modules are missing.
# isort: off
from llmfoundry.models.utils.adapt_tokenizer import (
    AutoTokenizerForMOD,  # type: ignore (see note),
    adapt_tokenizer_for_denoising,  # type: ignore (see note)
)
from llmfoundry.models.utils.hf_prefixlm_converter import (
    add_bidirectional_mask_if_missing,  # type: ignore (see note)
    convert_hf_causal_lm_to_prefix_lm,  # type: ignore (see note)
)
from llmfoundry.models.utils.meta_init_context import \
    init_empty_weights  # type: ignore (see note)
from llmfoundry.models.utils.param_init_fns import (
    generic_param_init_fn_,  # type: ignore (see note)
    MODEL_INIT_REGISTRY,
)

try:
    from llmfoundry.models.layers.flash_attn_triton import flash_attn_func as flash_attn_func
except:
    pass
# isort: on

import logging

# JP: imports from mpt/modeling_mpt
from llmfoundry.models.mpt.modeling_mpt import MPTPreTrainedModel, MPTModel, MPTForCausalLM, ComposerMPTCausalLM

log = logging.getLogger(__name__)

# JP: Added for contrastive loss
# Todo: does this exist somewhere? Move this to a util?
def dist_gather_tensor(t: Optional[torch.Tensor]) -> Optional[torch.Tensor]:

    """
    This function applies an all gather operation necessary for the contrastive 
    InfoNCELoss applied in the ComposerMPTContrastiveLM class
    """
    if t is None:
        return None

    t = t.contiguous()
    all_tensors = [torch.empty_like(t) for _ in range(dist.get_world_size())]
    torch.distributed.all_gather(all_tensors, t)

    all_tensors[dist.get_global_rank()] = t
    all_tensors = torch.cat(all_tensors, dim=0)
    return all_tensors

class Artanh(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x):
        x = x.clamp(-1 + 1e-5, 1 - 1e-5)
        ctx.save_for_backward(x)
        res = (torch.log_(1 + x).sub_(torch.log_(1 - x))).mul_(0.5)
        return res

    @staticmethod
    def backward(ctx, grad_output):
        (input,) = ctx.saved_tensors
        return grad_output / (1 - input ** 2)

class ComposerMPTContrastiveLM(HuggingFaceModel):

    """ 
    JP: The following code implements a contrastive loss function using the MPT
    architecture.

    Note that the MPTForCausalLM architecture can be used as is, provided that `labels=None`
    Most of the code in this class is modeled off ComposerMPTCausalLM
    In the init, we use the MPTForCausalLM as the main model

    attention mask

    bidirectional mask

    The main addition is the function _compute_scores() in the forward pass, which implements the contrastive loss
    """

    def __init__(
        self,
        om_model_config: DictConfig,
        tokenizer: Optional[PreTrainedTokenizerBase] = None,
    ):
        resolved_om_model_config = om.to_container(om_model_config,
                                                   resolve=True)
        hf_config = MPTConfig.from_dict(resolved_om_model_config)
        model = MPTForCausalLM(hf_config)

        use_train_metrics = om_model_config.get('use_train_metrics', True)
        
        # train_metrics = [LanguageCrossEntropy(),
        #                  LanguagePerplexity()] if use_train_metrics else []

        # JP Add
        train_metrics = [LanguageCrossEntropy()] if use_train_metrics else []

        # JP: These metrics might not work for the contrastive loss
        # eval_metrics = [
        #     LanguageCrossEntropy(),
        #     LanguagePerplexity(),
        #     InContextLearningLMAccuracy(),
        #     InContextLearningMultipleChoiceAccuracy(),
        #     InContextLearningQAAccuracy(),
        #     InContextLearningCodeEvalAccuracy(),
        #     InContextLearningLMExpectedCalibrationError(),
        #     InContextLearningMCExpectedCalibrationError(),
        # ]
        eval_metrics = []

        super().__init__(
            model=model,
            tokenizer=tokenizer,
            use_logits=False, # JP: set to False
            metrics=train_metrics,
            eval_metrics=eval_metrics, # might be worth setting to []
            shift_labels=False, # JP: set to False
            allow_embedding_resizing=True, # JP: Not sure what this does. was set to True
        )

        # Temperature for InfoNCELoss
        self.temperature = resolved_om_model_config.get('temperature', 1)

        self.n_active_params = sum(p.numel() for p in self.parameters())

        loss_fn_config = om_model_config.get('loss_fn', 'fused_crossentropy')
        if loss_fn_config == 'fused_crossentropy':
            try:
                from flash_attn.losses.cross_entropy import \
                    CrossEntropyLoss as FusedCrossEntropyLoss

                self.loss_fn = FusedCrossEntropyLoss(ignore_index=-100)
            except:
                raise ValueError(
                    'Fused Cross Entropy is not installed. Either (1) have a CUDA-compatible GPU '
                    +
                    'and `pip install .[gpu]` if installing from source or `pip install xentropy-cuda-lib@git+https://github.com/HazyResearch/flash-attention.git@v1.0.3#subdirectory=csrc/xentropy` '
                    +
                    'if installing from pypi, or (2) set your config model.loss_fn=torch_crossentropy.'
                )
        elif loss_fn_config == 'torch_crossentropy':
            self.loss_fn = nn.CrossEntropyLoss(ignore_index=-100)
        else:
            raise ValueError(
                f'Specified loss_fn={self.loss_fn} not recognized. `loss_fn` must be one of [`fused_crossentropy`, `torch_crossentropy`].'
            )

    def format_queries_batch(self, batch, last_hidden_state):
        """ Format `queries` by selecting every other pair from the batch 
        JP: Note that there could be a better way to do this
        """
        queries = {}
        step_size = self.config.to_dict().get("pos_step_size", 2)
        for key in batch.keys():
            queries[key] = batch[key][0::step_size, :] # no need to reshape.reshape(batch[key].size(0), -1)
            
        return queries, last_hidden_state[0::step_size, :, :]
    
    def format_passages_batch(self, batch, last_hidden_state):
        """ Format `passages` by selecting every other pair from the batch """
        passages = {}
        step_size = self.config.to_dict().get("pos_step_size", 2)

        for key in batch.keys():
            pattern = torch.arange(1, step_size)
            num_blocks = batch[key].size(0) // step_size
            index = pattern + torch.arange(0, num_blocks * step_size, step_size).unsqueeze(1)
            index = index.view(-1)
            passages[key] = batch[key][index] #.reshape(batch[key].size(0), -1)
        
        return passages, last_hidden_state[index, :, :]

    def forward(self, batch: MutableMapping) -> CausalLMOutputWithPast:
        if self.model.transformer.prefix_lm:
            add_bidirectional_mask_if_missing(batch)
        # Note: prefix_mask is only used if model.prefix_lm is True

        # JP - add
        # Reshape pairs so that 
        dim1,dim2,dim3=batch['input_ids'].shape
        # print(batch['input_ids'].shape)
        # print(batch['input_ids'].reshape((dim1*dim2,dim3)))
        reshaped_input_ids = batch['input_ids'].reshape((dim1*dim2,dim3))
        batch['input_ids'] = reshaped_input_ids
        
        # still need TO DO for prefix_mask and input_embeds
        if 'attention_mask' in batch:
            reshaped_attention_mask = batch['attention_mask'].reshape((dim1*dim2,dim3))
            batch['attention_mask'] = reshaped_attention_mask
            # print('attention mask shape: ', batch['attention_mask'].shape)
        if 'prefix_mask' in batch:
            reshaped_prefix_mask = batch['prefix_mask'].reshape((dim1*dim2,dim3))
            batch['prefix_mask'] = reshaped_prefix_mask
        if 'sequence_id' in batch:
            reshaped_sequence_id = batch['sequence_id'].reshape((dim1*dim2,dim3))
            batch['sequence_id'] = reshaped_sequence_id
        if 'input_embeds' in batch:
            reshaped_input_embeds = batch['input_embeds'].reshape((dim1*dim2,dim3))
            batch['input_embeds'] = reshaped_input_embeds
        
        if 'labels' in batch:
            reshaped_labels = batch['labels'].reshape((dim1*dim2,dim3))
            batch['labels'] = reshaped_labels

        return self.model(
            input_ids=batch['input_ids'],
            attention_mask=batch.get('attention_mask', None),
            prefix_mask=batch.get('prefix_mask', None),
            sequence_id=batch.get('sequence_id', None),
            inputs_embeds=batch.get('inputs_embeds', None),
        )
    
    def _compute_scores(self, batch, outputs) -> Tuple:

        """
        Run Pairs through the encoder separately in two passes, designated as q (query) and p (passage)
        [batch_size, sequence_length]
        
        the pooled_outputs is [batch_size, hidden_size]
        
        Note: at some future point we could use the flag 'token_type_ids' which was used in the original
        BERT formula to keep track of sentences A and sentences B in the next sentence prediction objective
        function. For now we split even and odd rows
        """
        (queries_batch, queries_last_hidden_state) = self.format_queries_batch(batch, outputs.hidden_states)
        (passages_batch, passages_last_hidden_state) = self.format_passages_batch(batch, outputs.hidden_states)
        
        
        # print(self.tokenizer.decode(queries_batch['input_ids'][0]))
        # print(self.tokenizer.decode(passages_batch['input_ids'][0]))

        # the output of self.model should be a @dataclass container with values
        # loss, logits, attentions, and hidden_states, which contains Hidden-states of the model at the 
        # output of each layer plus the optional initial embedding outputs.
        #
        # in order to access the final output of the hidden states, we can do hidden_states[-1]
        
        # JP: Note we went from p_encoder_outputs.masked_fill to q_encoder_outputs.hidden_states.masked_fill
        # Might need to do hidden_states[-1] to only select activations from the last layer
        #
        # Note that we do _not_ want to use the logits; we want the full output dimension to be the hidden_dim
        # for batch of [16,128], q_encoder_outputs.hidden_states has shape [16,128,768] but q_encoder_outputs.hidden_states[-1].shape:  torch.Size([128, 768])
        # q_last_hidden also has shape [16,128,768]. q_pooled_outputs should be [16,768]
        
        # print('\nq_encoder_outputs.hidden_states[-1].shape: ', q_encoder_outputs.hidden_states[-1].shape)
        q_last_hidden = queries_last_hidden_state.masked_fill(~queries_batch.get('attention_mask', None)[..., None].bool(), 0.0)
        q_pooled_outputs = q_last_hidden.sum(dim=1) / queries_batch.get('attention_mask', None).sum(dim=1)[..., None]
        
        p_last_hidden = passages_last_hidden_state.masked_fill(~passages_batch.get('attention_mask', None)[..., None].bool(), 0.0)
        p_pooled_outputs = p_last_hidden.sum(dim=1) / passages_batch.get('attention_mask', None).sum(dim=1)[..., None]
        
        #print('>>p_pooled_outputs shape:',p_pooled_outputs.shape)
        
        q_pooled_outputs = F.normalize(q_pooled_outputs, dim=-1) # Todo: should be configurable when L2 normalizing
        p_pooled_outputs = F.normalize(p_pooled_outputs, dim=-1)

        q_pooled_outputs = q_pooled_outputs.contiguous() # Why do we need to make this contiguous?
        p_pooled_outputs = p_pooled_outputs.contiguous() # Why do we need to make this contiguous?

        # All Gather is included
        all_q_pooled_outputs = dist_gather_tensor(q_pooled_outputs)
        all_p_pooled_outputs = dist_gather_tensor(p_pooled_outputs)
        
        # No All Gather
        #all_q_pooled_outputs = q_pooled_outputs
        #all_p_pooled_outputs = p_pooled_outputs
        
        all_scores, all_labels = self.full_contrastive_scores_and_labels(queries=all_q_pooled_outputs, 
                                                                         passages=all_p_pooled_outputs)
        
        scale = 1 / self.temperature
        
        all_scores = all_scores * scale
        
        # start = dist.get_global_rank() * q_pooled_outputs.shape[0]
        
        # local_query_indices = torch.arange(start, start + q_pooled_outputs.shape[0], dtype=torch.long).to(q_pooled_outputs.device)
        
        # scores = all_scores.index_select(dim=0, index=local_query_indices)
        # all_scores[dist.get_global_rank()] = scores
        # labels = all_labels.index_select(dim=0, index=local_query_indices)s
        # scores = all_scores
        # labels = all_labels
        #print('>>labels',labels.shape) # should be torch.Size([64])

        print('>> mean scores',all_scores.mean())
        print('>> mean diagonal scores', all_scores.diagonal().mean())
        return all_scores, all_labels
    
    def dist(self, x, y, *, c=1.0, keepdim=False):
        r"""
        Distance on the Poincare ball
        .. math::
            d_c(x, y) = \frac{2}{\sqrt{c}}\tanh^{-1}(\sqrt{c}\|(-x)\oplus_c y\|_2)
        .. plot:: plots/extended/poincare/distance.py
        Parameters
        ----------
        x : tensor
            point on poincare ball
        y : tensor
            point on poincare ball
        c : float|tensor
            ball negative curvature
        keepdim : bool
            retain the last dim? (default: false)
        Returns
        -------
        tensor
            geodesic distance between :math:`x` and :math:`y`
        """
        c = torch.as_tensor(c).type_as(x)
        return self._dist(x, y, c, keepdim=keepdim)
    
    def _dist(self,x, y, c, keepdim: bool = False):
        sqrt_c = c ** 0.5
        dist_c = self.artanh(sqrt_c * self._mobius_add(-x, y, c).norm(dim=-1, p=2, keepdim=keepdim))
        return dist_c * 2 / sqrt_c
    
    def artanh(self, x):
        return Artanh.apply(x)
    
    def mobius_add(self, x, y, *, c=1.0):
        r"""
        Mobius addition is a special operation in a hyperbolic space.
        .. math::
            x \oplus_c y = \frac{
                (1 + 2 c \langle x, y\rangle + c \|y\|^2_2) x + (1 - c \|x\|_2^2) y
                }{
                1 + 2 c \langle x, y\rangle + c^2 \|x\|^2_2 \|y\|^2_2
            }
        In general this operation is not commutative:
        .. math::
            x \oplus_c y \ne y \oplus_c x
        But in some cases this property holds:
        * zero vector case
        .. math::
            \mathbf{0} \oplus_c x = x \oplus_c \mathbf{0}
        * zero negative curvature case that is same as Euclidean addition
        .. math::
            x \oplus_0 y = y \oplus_0 x
        Another usefull property is so called left-cancellation law:
        .. math::
            (-x) \oplus_c (x \oplus_c y) = y
        Parameters
        ----------
        x : tensor
            point on the Poincare ball
        y : tensor
            point on the Poincare ball
        c : float|tensor
            ball negative curvature
        Returns
        -------
        tensor
            the result of mobius addition
        """
        c = torch.as_tensor(c).type_as(x)
        return self._mobius_add(x, y, c)


    def _mobius_add(self, x, y, c):
        x2 = x.pow(2).sum(dim=-1, keepdim=True)
        y2 = y.pow(2).sum(dim=-1, keepdim=True)
        xy = (x * y).sum(dim=-1, keepdim=True)
        num = (1 + 2 * c * xy + c * y2) * x + (1 - c * x2) * y
        denom = 1 + 2 * c * xy + c ** 2 * x2 * y2
        return num / (denom + 1e-5)

    def project(self, x, *, c=1.0):
        r"""
        Safe projection on the manifold for numerical stability. This was mentioned in [1]_
        Parameters
        ----------
        x : tensor
            point on the Poincare ball
        c : float|tensor
            ball negative curvature
        Returns
        -------
        tensor
            projected vector on the manifold
        References
        ----------
        .. [1] Hyperbolic Neural Networks, NIPS2018
            https://arxiv.org/abs/1805.09112
        """
        c = torch.as_tensor(c).type_as(x).to(x.device)
        return self._project(x, c)

    def _project(self, x, c):
        norm = torch.clamp_min(x.norm(dim=-1, keepdim=True, p=2), 1e-5)
        maxnorm = (1 - 1e-3) / (c ** 0.5)
        cond = norm > maxnorm
        projected = x / norm * maxnorm
        return torch.where(cond, projected, x)

    def poincare_distance(self, queries: torch.Tensor, passages: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
         # the labels 
        
        # this calculates the inner product between query and passage pairs
        # qp = torch.mm(queries, passages.t())
        # numerator = F.normalize(queries - passages, dim=-1) ** 2
        # denominator = (1 - queries ** 2) * (1 - passages ** 2)
        # qp = torch.acosh(1 + 2 * numerator / denominator)
        # Note: from https://github.com/HazyResearch/hyperbolics/blob/master/pytorch/hyperbolic_models.py#L52
        hyperbolic_queries = self.project(queries)
        hyperbolic_passages = self.project(passages)
        
        z  = 2 * (torch.norm( hyperbolic_queries - hyperbolic_passages, 2, 1)**2)
        uu = 1. + torch.div(z, ( ( 1 - torch.norm(hyperbolic_queries, 2, 1) ** 2 ) * ( 1 - torch.norm(hyperbolic_passages, 2, 1) ** 2 )))
        # # machine_eps = np.finfo(uu.data.numpy().dtype).eps  # problem with cuda tensor
        # # return acosh(torch.clamp(uu, min=1+machine_eps))
        score = torch.acosh(uu.clamp(min=1+1e-5))
        
        return score

    
    def full_contrastive_scores_and_labels(self, queries: torch.Tensor, passages: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:

        # the labels 
        labels = torch.arange(0, passages.shape[0], dtype=torch.long, device=passages.device)
        
        # this calculates the inner product between query and passage pairs
        qp = torch.mm(queries, passages.t())

        #print('>> qp shape:', qp.shape)

        return qp, labels
    
    def poincare_contrastive_loss(self, batch, outputs, margin=1.0):
        (queries_batch, queries_last_hidden_state) = self.format_queries_batch(batch, outputs.hidden_states)
        (passages_batch, passages_last_hidden_state) = self.format_passages_batch(batch, outputs.hidden_states)
        
        q_last_hidden = queries_last_hidden_state.masked_fill(~queries_batch.get('attention_mask', None)[..., None].bool(), 0.0)
        q_pooled_outputs = q_last_hidden.sum(dim=1) / queries_batch.get('attention_mask', None).sum(dim=1)[..., None]
        
        p_last_hidden = passages_last_hidden_state.masked_fill(~passages_batch.get('attention_mask', None)[..., None].bool(), 0.0)
        p_pooled_outputs = p_last_hidden.sum(dim=1) / passages_batch.get('attention_mask', None).sum(dim=1)[..., None]
        
        all_q_pooled_outputs = dist_gather_tensor(q_pooled_outputs)
        all_p_pooled_outputs = dist_gather_tensor(p_pooled_outputs)

        distances = self.poincare_distance(q_pooled_outputs.to(dtype=torch.float64), all_p_pooled_outputs.to(dtype=torch.float64))
        labels = torch.cat([torch.ones(1), torch.zeros(all_p_pooled_outputs.size(0) - 1)])
        labels = labels.to(device=distances.device)
                        #    .repeat(all_q_pooled_outputs.size(0) // p_pooled_outputs.size(0))).to(device=distances.device)
        losses = labels * distances.pow(2) + (1 - labels) * F.relu(margin - distances).pow(2)
        return losses.mean(), labels
        # return distances

    def loss(self, outputs: CausalLMOutputWithPast,
             batch: Mapping) -> torch.Tensor:
        
        loss, labels = self.poincare_contrastive_loss(batch, outputs)
        # scores, labels = self._compute_scores(batch, outputs)
                
        # loss = self.loss_fn(scores.unsqueeze(0), labels)

        self.labels = labels # JP Added, necessary for train metrics LanguageCrossEntropy
        # Note that LanguageCrossEntropy() calculates loss with respect to logits
        # e.g. losses = self.loss_fn(logits, target)
        # This is different from how we are calculating the loss between the output vectors of query vs passage

        
        # Based on https://github.com/microsoft/unilm/blob/b60c741f746877293bb85eed6806736fc8fa0ffd/simlm/src/models/biencoder_model.py#L60C62-L60C62
        # We are scaling the loss by the world size because we think it will be divided by the world size in the backward pass
        # This is a hacky way of getting around implementing our own backward pass
        # loss *= dist.get_world_size()
        
        return loss # do we also need to pass the labels and the scores?
    
        # {
        #     'loss': loss,
        #     'logits': scores, # This doesn't seem right, but needs to be here for torchmetrics
        #     'labels': labels
        # }

    def flops_per_batch(self, batch: Mapping) -> int:
        # Note: this computation does not take into account padding, and assumes
        # that the dataset has been constructed without padding. Additionally, we
        # assume the backward pass is approximately 2x the forward pass

        bs, msl = batch['input_ids'].shape[0:2]
        params_flops_per_token = 2 * self.n_active_params
        params_flops_per_seq = params_flops_per_token * msl
        attn_flops_per_seq = (self.model.config.n_layers * 2 * 2 *
                              (self.model.config.d_model * (msl**2)))

        return (params_flops_per_seq + attn_flops_per_seq) * 3 * bs
