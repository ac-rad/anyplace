import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

def clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])

def attention(query, key, value, mask=None):
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1).contiguous()) / (d_k ** 0.5)
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    p_attn = F.softmax(scores, dim=-1)
    return torch.matmul(p_attn, value), p_attn

class EncoderDecoder(nn.Module):
    def __init__(self, encoder, decoder, src_embed, tgt_embed, generator):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.src_embed = src_embed
        self.tgt_embed = tgt_embed
        self.generator = generator

    def encode(self, src, src_mask):
        return self.encoder(self.src_embed(src), src_mask)

    def decode(self, memory, src_mask, tgt, tgt_mask):
        return self.generator(self.decoder(self.tgt_embed(tgt), memory, src_mask, tgt_mask))

    def forward(self, src, tgt, src_mask, tgt_mask):
        return self.decode(self.encode(src, src_mask), src_mask, tgt, tgt_mask)

class Encoder(nn.Module):
    def __init__(self, layer, N):
        super().__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, mask):
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)

class Decoder(nn.Module):
    def __init__(self, layer, N):
        super().__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, memory, src_mask, tgt_mask):
        for layer in self.layers:
            x = layer(x, memory, src_mask, tgt_mask)
        return self.norm(x)

class DecoderMultiQuery(nn.Module):
    def __init__(self, layer, N, return_intermediate=False):
        super().__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)
        self.return_intermediate = return_intermediate

    def with_pos_embed(self, tensor, pos=None):
        return tensor if pos is None else tensor + pos

    def forward(self, x, memory, src_mask, tgt_mask, pos=None, query_pos=None):

        intermediate = []

        for layer in self.layers:
            x = layer(x, memory, src_mask, tgt_mask, pos=pos, query_pos=query_pos)

            if self.return_intermediate:
                intermediate.append(self.norm(x))

        if self.return_intermediate:
            output = self.norm(x)
            intermediate.pop()
            intermediate.append(output)
            return torch.stack(intermediate)

        return self.norm(x)

class LayerNorm(nn.Module):
    def __init__(self, features, eps=1e-6):
        super().__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2

class SublayerConnection(nn.Module):
    def __init__(self, size):
        super().__init__()
        self.norm = LayerNorm(size)

    def forward(self, x, sublayer):
        return x + sublayer(self.norm(x))

class EncoderLayer(nn.Module):
    def __init__(self, size, self_attn, feed_forward):
        super(EncoderLayer, self).__init__()
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size), 2)
        self.size = size

    def forward(self, x, mask):
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        return self.sublayer[1](x, self.feed_forward)

class DecoderLayer(nn.Module):

    def __init__(self, size, self_attn, src_attn, feed_forward):
        super().__init__()
        self.size = size
        self.self_attn = self_attn
        self.src_attn = src_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size), 3)

    def forward(self, x, memory, src_mask, tgt_mask):
        m = memory
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, tgt_mask))
        x = self.sublayer[1](x, lambda x: self.src_attn(x, m, m, src_mask))
        return self.sublayer[2](x, self.feed_forward)

class DecoderLayerPosEmbed(nn.Module):

    def __init__(self, size, self_attn, src_attn, feed_forward):
        super().__init__()
        self.size = size
        self.self_attn = self_attn
        self.src_attn = src_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size), 3)

    def with_pos_embed(self, tensor, pos=None):
        return tensor if pos is None else tensor + pos

    def forward(self, x, memory, src_mask, tgt_mask, pos=None, query_pos=None):
        # q = k = self.with_pos_embed(x, query_pos)
        # v = x
        m = memory
        x = self.sublayer[0](x, lambda x: self.self_attn(self.with_pos_embed(x, query_pos), self.with_pos_embed(x, query_pos), x, tgt_mask))
        x = self.sublayer[1](x, lambda x: self.src_attn(self.with_pos_embed(x, query_pos), self.with_pos_embed(m, pos) , m, src_mask))
        return self.sublayer[2](x, self.feed_forward)

class MultiHeadedAttention(nn.Module):
    
    def __init__(self, h, d_model):
        super().__init__()
        assert d_model % h == 0
        # We assume d_v always equals d_k
        self.d_k = d_model // h
        self.h = h
        self.linears = clones(nn.Linear(d_model, d_model), 4)

    def forward(self, query, key, value, mask=None):
        if mask is not None:
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)

        # 1) Do all the linear projections in batch from d_model => h x d_k
        query, key, value = \
            [l(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2).contiguous()
             for l, x in zip(self.linears, (query, key, value))]

        # 2) Apply attention on all the projected vectors in batch.
        x, self.attn = attention(query, key, value, mask=mask)

        # 3) "Concat" using a view and apply a final linear.
        x = x.transpose(1, 2).contiguous().view(nbatches, -1, self.h * self.d_k)

        return self.linears[-1](x)

class PositionwiseFeedForward(nn.Module):

    def __init__(self, d_model, d_ff):
        super().__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.norm = nn.Sequential()
        self.w_2 = nn.Linear(d_ff, d_model)

    def forward(self, x):
        return self.w_2(self.norm(F.relu(self.w_1(x)).transpose(2, 1).contiguous()).transpose(2, 1).contiguous())

class Transformer(nn.Module):

    def __init__(self, cfg):
        super().__init__()
        c = copy.deepcopy
        attn = MultiHeadedAttention(
            cfg.model.num_heads, 
            cfg.model.pc_feat_dim
        )

        ff = PositionwiseFeedForward(
            cfg.model.pc_feat_dim, 
            cfg.model.transformer_feat_dim
        )

        self.model = EncoderDecoder(
            Encoder(EncoderLayer(cfg.model.pc_feat_dim, c(attn), c(ff)), cfg.model.num_blocks),
            Decoder(DecoderLayer(cfg.model.pc_feat_dim, c(attn), c(attn), c(ff)), cfg.model.num_blocks),
            nn.Sequential(),                        
            nn.Sequential(),                        
            nn.Sequential()
        )

    def forward(self, src, tgt):
        src = src.transpose(2, 1).contiguous()
        tgt = tgt.transpose(2, 1).contiguous()
        src_corr_feat = self.model(tgt, src, None, None).transpose(2, 1).contiguous()
        return src_corr_feat


class TransformerMultiQuery(nn.Module):

    def __init__(self, cfg):
        super().__init__()
        c = copy.deepcopy
        attn = MultiHeadedAttention(
            cfg.model.num_heads, 
            cfg.model.pc_feat_dim
        )

        ff = PositionwiseFeedForward(
            cfg.model.pc_feat_dim, 
            cfg.model.transformer_feat_dim
        )

        self.model = EncoderDecoder(
            Encoder(EncoderLayer(cfg.model.pc_feat_dim, c(attn), c(ff)), cfg.model.num_blocks),
            Decoder(DecoderLayer(cfg.model.pc_feat_dim, c(attn), c(attn), c(ff)), cfg.model.num_blocks),
            nn.Sequential(),                        
            nn.Sequential(),                        
            nn.Sequential()
        )
        
        n_queries = cfg.model.n_queries
        h_dim = cfg.model.pc_feat_dim
        init_param_values = torch.randn(1, n_queries, h_dim) / np.sqrt(h_dim)
        self.query_embed = nn.Parameter(init_param_values)
        self.dec_mq = DecoderMultiQuery(
            DecoderLayerPosEmbed(
                cfg.model.pc_feat_dim, 
                c(attn), 
                c(attn), 
                c(ff)
            ), 
            cfg.model.num_blocks, 
            return_intermediate=cfg.model.return_intermediate)

    def forward(self, src, tgt):
        src = src.transpose(2, 1).contiguous()
        tgt = tgt.transpose(2, 1).contiguous()
        src_corr_feat = self.model(tgt, src, None, None)

        B = src.shape[0]
        query_embed = self.query_embed.repeat((B, 1, 1))
        query_token = torch.zeros_like(query_embed)
        out = self.dec_mq(query_token, src_corr_feat, None, None, query_pos=query_embed)
        if out.dim() == 4:
            out = out.permute(1, 0, 2, 3)
        return out
