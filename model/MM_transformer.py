from collections import OrderedDict
import torch
from torch import nn
from timm.models.layers import DropPath

from model.modules.attention import Attention
from model.modules.graph import GCN
from model.modules.mlp import MLP
from model.modules.tcn import MultiScaleTCN


class AGFormerBlock(nn.Module):
    """
    Implementation of AGFormer block.
    """
    def __init__(self, dim, mlp_ratio=4., act_layer=nn.GELU, attn_drop=0., drop=0., drop_path=0.,
                 num_heads=8, qkv_bias=False, qk_scale=None, use_layer_scale=True, layer_scale_init_value=1e-5,
                 mode='spatial', mixer_type="attention", use_temporal_similarity=True,
                 temporal_connection_len=1, neighbour_num=4, n_frames=243):
        super().__init__()
        self.norm1 = nn.LayerNorm(dim)
        if mixer_type == 'attention':
            self.mixer = Attention(dim, dim, num_heads, qkv_bias, qk_scale, attn_drop,
                                   proj_drop=drop, mode=mode)
        elif mixer_type == 'graph':
            self.mixer = GCN(dim, dim,
                             num_nodes=24 if mode == 'spatial' else n_frames,
                             neighbour_num=neighbour_num,
                             mode=mode,
                             use_temporal_similarity=use_temporal_similarity,
                             temporal_connection_len=temporal_connection_len)
        elif mixer_type == "ms-tcn":
            self.mixer = MultiScaleTCN(in_channels=dim, out_channels=dim)
        else:
            raise NotImplementedError("AGFormer mixer_type is either attention or graph")
        self.norm2 = nn.LayerNorm(dim)

        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = MLP(in_features=dim, hidden_features=mlp_hidden_dim,
                       act_layer=act_layer, drop=drop)

        # Techniques to help training deep models.
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.use_layer_scale = use_layer_scale
        if use_layer_scale:
            self.layer_scale_1 = nn.Parameter(layer_scale_init_value * torch.ones(dim), requires_grad=True)
            self.layer_scale_2 = nn.Parameter(layer_scale_init_value * torch.ones(dim), requires_grad=True)

    def forward(self, x, mask=None):
        """
        x: tensor with shape [B, T, J, C]
        """
        if self.use_layer_scale:
            x = x + self.drop_path(
                self.layer_scale_1.unsqueeze(0).unsqueeze(0)
                * self.mixer(self.norm1(x), mask = mask))
            x = x + self.drop_path(
                self.layer_scale_2.unsqueeze(0).unsqueeze(0)
                * self.mlp(self.norm2(x)))
        else:
            x = x + self.drop_path(self.mixer(self.norm1(x), mask = mask))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class MotionAGFormerBlock(nn.Module):
    """
    Implementation of MotionAGFormer block.
    It has two branches (attention-based and graph-based) followed by adaptive fusion.
    """
    def __init__(self, dim, mlp_ratio=4., act_layer=nn.GELU, attn_drop=0., drop=0., drop_path=0.,
                 num_heads=8, use_layer_scale=True, qkv_bias=False, qkv_scale=None, layer_scale_init_value=1e-5,
                 use_adaptive_fusion=True, hierarchical=False, use_temporal_similarity=True,
                 temporal_connection_len=1, use_tcn=False, graph_only=False, neighbour_num=4, n_frames=243):
        super().__init__()
        self.hierarchical = hierarchical
        dim_branch = dim // 2 if hierarchical else dim

        # Attention branch (spatial then temporal)
        self.att_spatial = AGFormerBlock(dim_branch, mlp_ratio, act_layer, attn_drop, drop, drop_path, num_heads,
                                         qkv_bias, qkv_scale, use_layer_scale, layer_scale_init_value,
                                         mode='spatial', mixer_type="attention",
                                         use_temporal_similarity=use_temporal_similarity,
                                         neighbour_num=neighbour_num, n_frames=n_frames)
        self.att_temporal = AGFormerBlock(dim_branch, mlp_ratio, act_layer, attn_drop, drop, drop_path, num_heads,
                                          qkv_bias, qkv_scale, use_layer_scale, layer_scale_init_value,
                                          mode='temporal', mixer_type="attention",
                                          use_temporal_similarity=use_temporal_similarity,
                                          neighbour_num=neighbour_num, n_frames=n_frames)

        # Graph branch (using graph mixer or TCN)
        if graph_only:
            self.graph_spatial = GCN(dim_branch, dim_branch, num_nodes=17, mode='spatial')
            if use_tcn:
                self.graph_temporal = MultiScaleTCN(in_channels=dim_branch, out_channels=dim_branch)
            else:
                self.graph_temporal = GCN(dim_branch, dim_branch, num_nodes=n_frames,
                                          neighbour_num=neighbour_num, mode='temporal',
                                          use_temporal_similarity=use_temporal_similarity,
                                          temporal_connection_len=temporal_connection_len)
        else:
            self.graph_spatial = AGFormerBlock(dim_branch, mlp_ratio, act_layer, attn_drop, drop, drop_path, num_heads,
                                               qkv_bias, qkv_scale, use_layer_scale, layer_scale_init_value,
                                               mode='spatial', mixer_type="graph",
                                               use_temporal_similarity=use_temporal_similarity,
                                               temporal_connection_len=temporal_connection_len,
                                               neighbour_num=neighbour_num, n_frames=n_frames)
            self.graph_temporal = AGFormerBlock(dim_branch, mlp_ratio, act_layer, attn_drop, drop, drop_path, num_heads,
                                                qkv_bias, qkv_scale, use_layer_scale, layer_scale_init_value,
                                                mode='temporal', mixer_type="ms-tcn" if use_tcn else 'graph',
                                                use_temporal_similarity=use_temporal_similarity,
                                                temporal_connection_len=temporal_connection_len,
                                                neighbour_num=neighbour_num, n_frames=n_frames)

        self.use_adaptive_fusion = use_adaptive_fusion
        if self.use_adaptive_fusion:
            self.fusion = nn.Linear(dim_branch * 2, 2)
            self._init_fusion()

    def _init_fusion(self):
        self.fusion.weight.data.fill_(0)
        self.fusion.bias.data.fill_(0.5)

    def forward(self, x, mask=None):
        """
        x: tensor with shape [B, T, J, C]
        """
        if self.hierarchical:
            B, T, J, C = x.shape
            x_attn, x_graph = x[..., :C // 2], x[..., C // 2:]
            x_attn = self.att_temporal(self.att_spatial(x_attn), mask=mask)
            x_graph = self.graph_temporal(self.graph_spatial(x_graph + x_attn), mask=mask)
        else:
            x_attn = self.att_temporal(self.att_spatial(x), mask=mask)
            x_graph = self.graph_temporal(self.graph_spatial(x), mask=mask)
        
        if self.hierarchical:
            x = torch.cat((x_attn, x_graph), dim=-1)
        elif self.use_adaptive_fusion:
            alpha = torch.cat((x_attn, x_graph), dim=-1)
            alpha = self.fusion(alpha)
            alpha = alpha.softmax(dim=-1)
            x = x_attn * alpha[..., 0:1] + x_graph * alpha[..., 1:2]
        else:
            x = (x_attn + x_graph) * 0.5

        return x


def create_layers(dim, n_layers, mlp_ratio=4., act_layer=nn.GELU, attn_drop=0., drop_rate=0., drop_path_rate=0.,
                  num_heads=8, use_layer_scale=True, qkv_bias=False, qkv_scale=None, layer_scale_init_value=1e-5,
                  use_adaptive_fusion=True, hierarchical=False, use_temporal_similarity=True,
                  temporal_connection_len=1, use_tcn=False, graph_only=False, neighbour_num=4, n_frames=243):
    """
    Generate a sequence of MotionAGFormer layers.
    """
    layers = []
    for _ in range(n_layers):
        layers.append(MotionAGFormerBlock(dim=dim,
                                          mlp_ratio=mlp_ratio,
                                          act_layer=act_layer,
                                          attn_drop=attn_drop,
                                          drop=drop_rate,
                                          drop_path=drop_path_rate,
                                          num_heads=num_heads,
                                          use_layer_scale=use_layer_scale,
                                          qkv_bias=qkv_bias,
                                          qkv_scale=qkv_scale,
                                          layer_scale_init_value=layer_scale_init_value,
                                          use_adaptive_fusion=use_adaptive_fusion,
                                          hierarchical=hierarchical,
                                          use_temporal_similarity=use_temporal_similarity,
                                          temporal_connection_len=temporal_connection_len,
                                          use_tcn=use_tcn,
                                          graph_only=graph_only,
                                          neighbour_num=neighbour_num,
                                          n_frames=n_frames))
    layers = nn.Sequential(*layers)
    return layers


class MotionAGFormer(nn.Module):
    """
    MotionAGFormer adapted for muscle activation prediction with demographic information.
    
    Inputs:
      - x: 3D pose sequence of shape [B, T, J, C] (e.g. [batch, time, joints, coordinates])
      - demo: demographic info of shape [B, demo_dim] (e.g. [gender, height, weight])
      
    The model embeds joint features, adds a learnable positional embedding, processes the sequence through MotionAGFormer blocks,
    then pools over joints and fuses the resulting representation with demographic info before predicting muscle activations per frame.
    """
    def __init__(self, n_layers, dim_in, dim_feat, dim_rep=512, dim_out=3, muscle_dim=402, mlp_ratio=4, act_layer=nn.GELU, attn_drop=0.,
                 drop=0., drop_path=0., use_layer_scale=True, layer_scale_init_value=1e-5, use_adaptive_fusion=True,
                 num_heads=4, qkv_bias=False, qkv_scale=None, hierarchical=False, num_joints=24,
                 use_temporal_similarity=True, temporal_connection_len=1, use_tcn=False, graph_only=False,
                 neighbour_num=4, n_frames=243, demo_dim=3):
        """
        Args:
            n_layers: Number of layers.
            dim_in: Input dimension per joint (e.g., 3 for 3D coordinates).
            dim_feat: Feature dimension for joint embedding.
            dim_rep: Intermediate representation dimension.
            muscle_dim: Output dimension (number of muscle activations, e.g., 402).
            num_joints: Number of joints in the pose (e.g., 52).
            demo_dim: Dimensionality of the demographic information (e.g., 3 for [gender, height, weight]).
            Other parameters as in the original MotionAGFormer.
        """
        super().__init__()
        self.num_joints = num_joints
        # Embed the input pose per joint.
        self.joints_embed = nn.Linear(dim_in, dim_feat)
        # Learnable positional embedding for joints.
        self.pos_embed = nn.Parameter(torch.zeros(1, num_joints, dim_feat))
        self.norm = nn.LayerNorm(dim_feat)

        # Create MotionAGFormer layers.
        self.layers = create_layers(dim=dim_feat,
                                    n_layers=n_layers,
                                    mlp_ratio=mlp_ratio,
                                    act_layer=act_layer,
                                    attn_drop=attn_drop,
                                    drop_rate=drop,
                                    drop_path_rate=drop_path,
                                    num_heads=num_heads,
                                    use_layer_scale=use_layer_scale,
                                    qkv_bias=qkv_bias,
                                    qkv_scale=qkv_scale,
                                    layer_scale_init_value=layer_scale_init_value,
                                    use_adaptive_fusion=use_adaptive_fusion,
                                    hierarchical=hierarchical,
                                    use_temporal_similarity=use_temporal_similarity,
                                    temporal_connection_len=temporal_connection_len,
                                    use_tcn=use_tcn,
                                    graph_only=graph_only,
                                    neighbour_num=neighbour_num,
                                    n_frames=n_frames)

        # Map joint features to an intermediate representation.
        self.rep_logit = nn.Sequential(OrderedDict([
            ('fc', nn.Linear(dim_feat, dim_rep)),
            ('act', nn.Tanh())
        ]))
        # Demographic embedding: map demo info to same representation dimension.
        self.demo_embed = nn.Linear(demo_dim, dim_rep)
        # Final head to predict muscle activations per frame.
        # We pool joint features over the joint dimension and then fuse with demo info.
        self.head = nn.Linear(dim_rep, muscle_dim)

    def forward(self, x, demo=None, mask=None, return_rep=False):
        """
        Args:
            x: Input tensor of shape [B, T, J, C] (e.g., [batch, time, joints, coordinates])
            demo: Demographic info tensor of shape [B, demo_dim]. If provided, it is fused with the motion representation.
            return_rep: If True, return the intermediate representation.
        Returns:
            Muscle activations of shape [B, T, muscle_dim]
        """
        # x: [B, T, J, C]
        x = self.joints_embed(x)          # [B, T, J, dim_feat]
        x = x + self.pos_embed            # Add positional embedding (broadcasted over B and T)
        for layer in self.layers:
            x = layer(x, mask=mask)                  # [B, T, J, dim_feat]
        x = self.norm(x)
        # Pool over joints: aggregate joint features per frame.
        x = x.mean(dim=2)                 # [B, T, dim_feat]
        # Get the intermediate motion representation.
        x = self.rep_logit(x)             # [B, T, dim_rep]
        # If demographic info is provided, fuse it into the representation.
        if demo is not None:
            # demo: [B, demo_dim] -> embed -> [B, dim_rep]
            demo_emb = self.demo_embed(demo)    # [B, dim_rep]
            # Expand along the time dimension.
            demo_emb = demo_emb.unsqueeze(1).expand(-1, x.shape[1], -1)  # [B, T, dim_rep]
            # Fuse via element-wise addition.
            x = x + demo_emb
        if return_rep:
            return x
        x = self.head(x)                  # [B, T, muscle_dim]
        return x


def _test():
    #from torchprofile import profile_macs
    import warnings
    warnings.filterwarnings('ignore')
    b, t, j, c = 1, 27, 52, 3  # e.g., batch=1, 27 frames, 52 joints, 3 coordinates per joint
    random_x = torch.randn((b, t, j, c)).to('cuda')
    # Demo information: e.g. [gender, height, weight]
    random_demo = torch.randn((b, 3)).to('cuda')

    model = MotionAGFormer(n_layers=12, dim_in=3, dim_feat=64, mlp_ratio=4, hierarchical=False,
                           use_tcn=False, graph_only=False, num_joints=j, muscle_dim=402, n_frames=t,
                           demo_dim=3).to('cuda')
    model.eval()

    model_params = sum(p.numel() for p in model.parameters())
    print(f"Model parameter #: {model_params:,}")
    #print(f"Model FLOPS #: {profile_macs(model, random_x, demo=random_demo):,}")

    # Warm-up
    for _ in range(10):
        _ = model(random_x, demo=random_demo)

    import time
    num_iterations = 100 
    start_time = time.time()
    with torch.no_grad():
        for _ in range(num_iterations):
            _ = model(random_x, demo=random_demo)
    end_time = time.time()
    average_inference_time = (end_time - start_time) / num_iterations
    fps = 1.0 / average_inference_time
    print(f"FPS: {fps}")

    out = model(random_x, demo=random_demo)
    # Expected output shape: [B, T, muscle_dim]
    assert out.shape == (b, t, 402), f"Output shape should be {b}x{t}x402 but it is {out.shape}"


if __name__ == '__main__':
    _test()
