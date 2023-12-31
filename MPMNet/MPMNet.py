import torch
from torch import nn
import torch.nn.functional as F
from .resnet import *
from .loss import WeightedDiceLoss
from .backbone_utils import Backbone
from .transformer import PositionEmbeddingSine
from .transformer import Transformer
from .FEM import FEM
from model.SPPM import ASPP
from model.correlation import Correlation
from functools import reduce
from operator import add
def Weighted_GAP(supp_feat, mask):
    supp_feat = supp_feat * mask
    feat_h, feat_w = supp_feat.shape[-2:][0], supp_feat.shape[-2:][1]
    area = F.avg_pool2d(mask, (supp_feat.size()[2], supp_feat.size()[3])) * feat_h * feat_w + 0.0005
    supp_feat = F.avg_pool2d(input=supp_feat, kernel_size=supp_feat.shape[-2:]) * feat_h * feat_w / area
    return supp_feat


class ProtoFormer(nn.Module):
    def __init__(self, layers=50, shot=1, reduce_dim=64, criterion=WeightedDiceLoss()):
        super(ProtoFormer, self).__init__()
        assert layers in [50, 101]
        self.layers = layers
        self.criterion = criterion
        self.shot = shot
        self.reduce_dim = reduce_dim
        # backbone_str =  'resnet' + str(self.layers)
        # if backbone_str == 'resnet50':
        #     self.feat_ids = list(range(3, 17))
        #     nbottlenecks = [3, 4, 6, 3]
        #     self.nsimlairy = [3,6,4]
        # elif backbone_str == 'resnet101':
        #     self.feat_ids = list(range(3, 34))
        #     nbottlenecks = [3, 4, 23, 3]
        #     self.nsimlairy = [3,23,4]
        # else:
        #     raise Exception('Unavailable backbone: %s' % backbone_str)
        # self.bottleneck_ids = reduce(add, list(map(lambda x: list(range(x)), nbottlenecks)))
        # self.lids = reduce(add, [[i + 1] * x for i, x in enumerate(nbottlenecks)])
        # self.stack_ids = torch.tensor(self.lids).bincount().__reversed__().cumsum(dim=0)[:3]
        # self.hyper_final = nn.Sequential(
        #     nn.Conv2d(sum(nbottlenecks[-3:]), 64, kernel_size=1, padding='same'),
        #     nn.BatchNorm2d(64),
        #     nn.ReLU(inplace=True)
        # )
        in_fea_dim = 1024 + 512

        drop_out = 0.5
        self.adjust_feature_supp = nn.Sequential(
            nn.Conv2d(in_fea_dim, reduce_dim, kernel_size=1, padding=0, bias=False),
            nn.BatchNorm2d(reduce_dim),
            nn.ReLU(inplace=True),
            nn.Dropout2d(p=drop_out),
        )
        self.adjust_feature_qry = nn.Sequential(
            nn.Conv2d(in_fea_dim, reduce_dim, kernel_size=1, padding=0, bias=False),
            nn.BatchNorm2d(reduce_dim),
            nn.ReLU(inplace=True),
            nn.Dropout2d(p=drop_out),
        )
        self.query_embed = nn.Embedding(reduce_dim, 1)
        self.pe_layer = PositionEmbeddingSine(reduce_dim // 2, normalize=True)
        self.transformer = Transformer(
            d_model=reduce_dim,
            dropout=0.1,
            nhead=4,
            dim_feedforward=reduce_dim // 4,
            num_encoder_layers=0,
            num_decoder_layers=1,
            normalize_before=False,
            return_intermediate_dec=False,
        )
        # self.fem = FEM(1536)
        self.high_avg_pool = nn.Identity()
        prior_channel = 1
        self.pyramid_bins = [60, 30, 15, 8]
        self.avgpool_list = []
        for bin in self.pyramid_bins:
            if bin > 1:
                self.avgpool_list.append(
                    nn.AdaptiveAvgPool2d(bin)
                )
        self.init_merge = []
        self.beta_conv = []
        for bin in self.pyramid_bins:
            self.init_merge.append(nn.Sequential(
                nn.Conv2d(reduce_dim * 2 + prior_channel*2, reduce_dim, kernel_size=1, padding=0, bias=False),
                nn.BatchNorm2d(reduce_dim),
                nn.ReLU(inplace=True)
            ))
            self.beta_conv.append(nn.Sequential(
                nn.Conv2d(reduce_dim, reduce_dim, kernel_size=3, padding=1, bias=False),
                nn.BatchNorm2d(reduce_dim),
                nn.ReLU(inplace=True),
                nn.Conv2d(reduce_dim, reduce_dim, kernel_size=3, padding=1, bias=False),
                nn.BatchNorm2d(reduce_dim),
                nn.ReLU(inplace=True)
            ))
        # self.gam = Attention(in_channels=64)
        self.init_merge = nn.ModuleList(self.init_merge)
        self.beta_conv = nn.ModuleList(self.beta_conv)
        self.res1 = nn.Sequential(
            nn.Conv2d(1280, reduce_dim, kernel_size=1, padding=0, bias=False)
        )
        self.res2 = nn.Sequential(
            nn.Conv2d(reduce_dim, reduce_dim, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(reduce_dim),
            nn.ReLU(inplace=True)
        )
        self.alpha_conv = []
        for idx in range(len(self.pyramid_bins) - 1):
            self.alpha_conv.append(nn.Sequential(
                nn.Conv2d(2 * reduce_dim, reduce_dim, kernel_size=1, stride=1, padding=0, bias=False),
                nn.BatchNorm2d(reduce_dim),
                nn.ReLU()
            ))
        self.alpha_conv = nn.ModuleList(self.alpha_conv)
        self.ASPP_meta = ASPP(256)
        self.init_weights()
        self.backbone = Backbone('resnet{}'.format(layers), train_backbone=False, return_interm_layers=True,
                                 dilation=[False, True, True])
    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if hasattr(m, 'bias') and m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def forward(self, x, s_x=torch.FloatTensor(1, 1, 3, 473, 473).cuda(), s_y = torch.FloatTensor(1, 1, 473, 473).cuda()):
        batch_size, C, h, w = x.size()
        img_size = x.size()[-2:]

        # backbone feature extraction
        qry_bcb_fts = self.backbone(x)
        supp_bcb_fts = self.backbone(s_x.view(-1, 3, *img_size))
        qry = self.backbone(x)
        supp = self.backbone(s_x.view(-1, 3, *img_size))
        # query_feat = torch.cat([qry_bcb_fts['1'], qry_bcb_fts['2']], dim=1)  # backbone的第一层和第二层的内容拼接
        # supp_feat = torch.cat([supp_bcb_fts['1'], supp_bcb_fts['2']], dim=1)  # 同上
        # query_feat = self.adjust_feature_qry(query_feat)  # 拼接后进行conv1x1
        # supp_feat = self.adjust_feature_supp(supp_feat)  # 同上
        fts_size = qry['1'].shape[-2:]
        # qry['2'],supp['2'] = self.fem(qry['2'],supp['2'])
        query_feat = torch.cat([qry_bcb_fts['1'], qry['2']], dim=1)  # backbone的第一层和第二层的内容拼接
        supp_feat = torch.cat([supp_bcb_fts['1'], supp['2']], dim=1)  # 同上
        # query_feat,supp_feat = self.fem(query_feat,supp_feat)
        query_feat = self.adjust_feature_qry(query_feat)  # 拼接后进行conv1x1
        supp_feat = self.adjust_feature_supp(supp_feat)
        supp_feat_list = []
        supp_feat_lists = []
        corrs = []
        gams = 0
        r_supp_feat = supp_feat.view(batch_size, self.shot, -1, fts_size[0], fts_size[1])
        for st in range(self.shot):
            mask = (s_y[:, st, :, :] == 1).float().unsqueeze(1)
            mask = F.interpolate(mask, size=(fts_size[0], fts_size[1]), mode='bilinear', align_corners=True)
            tmp_supp_feat = r_supp_feat[:, st, ...]
            tmp_supp_feat = Weighted_GAP(tmp_supp_feat, mask)
            supp_feat_list.append(tmp_supp_feat)
        #     gams += self.gam(tmp_supp_feat_max, mask)
        # gam = gams / self.shot
        #     supp_feat_lists.append(supp['2'])
        #     support_feats_1 = self.mask_feature(tmp_supp_feat_max, mask)

        #     corr = Correlation.multilayer_correlation(qry, support_feats_1, self.stack_ids)
        #     corrs.append(corr)
        # corrs_shot = [corrs[0][i] for i in range(len(self.nsimlairy))]
        # for ly in range(len(self.nsimlairy)):
        #     for s in range(1, self.shot):
        #         corrs_shot[ly] += (corrs[s][ly])
        # hyper_4 = corrs_shot[0] / self.shot
        # hyper_3 = corrs_shot[1] / self.shot
        # hyper_2 = corrs_shot[2] / self.shot
        # hyper_final = torch.cat([hyper_2, hyper_3, hyper_4], 1)
        # hyper_final = self.hyper_final(hyper_final)
        global_supp_pp = supp_feat_list[0]
        if self.shot > 1:
            for i in range(1, len(supp_feat_list)):
                global_supp_pp += supp_feat_list[i]
            global_supp_pp /= len(supp_feat_list)

        query_embed = global_supp_pp.squeeze(-1)
        query_pos = self.query_embed.weight
        key_pos = self.pe_layer(query_feat)
        key_embed = query_feat
        masking = None
        fg_embed = self.transformer(key_embed, masking, query_embed, query_pos, key_pos)
        # prior generation
        query_feat_high = qry_bcb_fts['3']
        supp_feat_high = supp_bcb_fts['3'].view(batch_size, -1, 2048, fts_size[0], fts_size[1])
        query_feat_middle = qry_bcb_fts['2']
        supp_feat_middle  = supp_bcb_fts['2'].view(batch_size, -1, 1024, fts_size[0], fts_size[1])
        corr_query_mask_1 = self.generate_prior(query_feat_high, supp_feat_high, s_y, fts_size)
        corr_query_mask_2 = self.generate_prior(query_feat_middle, supp_feat_middle, s_y, fts_size)
        corr_query_mask   = torch.cat([corr_query_mask_2,corr_query_mask_1],dim=1)
        pyramid_feat_list = []
        out_list = []
        for idx, tmp_bin in enumerate(self.pyramid_bins):
            if tmp_bin <= 1.0:
                bin = int(query_feat.shape[2] * tmp_bin)
                query_feat_bin = nn.AdaptiveAvgPool2d(bin)(query_feat)
            else:
                bin = tmp_bin
                query_feat_bin = self.avgpool_list[idx](query_feat)
            supp_feat_bin = global_supp_pp.expand(-1, -1, bin, bin)
            corr_mask_bin = F.interpolate(corr_query_mask, size=(bin, bin), mode='bilinear', align_corners=True)
            # gam = F.interpolate(gam, size=(bin, bin), mode='bilinear', align_corners=True)
            # hyper_final = F.interpolate(hyper_final, size=(bin, bin), mode='bilinear', align_corners=True)
            merge_feat_bin = torch.cat([query_feat_bin, supp_feat_bin, corr_mask_bin], 1)
            merge_feat_bin = self.init_merge[idx](merge_feat_bin)

            if idx >= 1:
                pre_feat_bin = pyramid_feat_list[idx - 1].clone()
                pre_feat_bin = F.interpolate(pre_feat_bin, size=(bin, bin), mode='bilinear', align_corners=True)
                rec_feat_bin = torch.cat([merge_feat_bin, pre_feat_bin], 1)
                merge_feat_bin = self.alpha_conv[idx - 1](rec_feat_bin) + merge_feat_bin

            merge_feat_bin = self.beta_conv[idx](merge_feat_bin) + merge_feat_bin
            inner_out_bin = merge_feat_bin
            merge_feat_bin = F.interpolate(merge_feat_bin, size=(query_feat.size(2), query_feat.size(3)),
                                           mode='bilinear', align_corners=True)
            pyramid_feat_list.append(merge_feat_bin)
            out_list.append(inner_out_bin)

        query_feat = torch.cat(pyramid_feat_list, 1)
        query_feat = self.ASPP_meta(query_feat)
        query_feat = self.res1(query_feat)
        query_feat = self.res2(query_feat)
        fused_query_feat = query_feat.clone()
        # Output Part
        out1 = torch.sigmoid(torch.einsum("bchw,bcl->blhw", fused_query_feat, fg_embed))
        out = F.interpolate(out1, size=(h, w), mode='bilinear', align_corners=True)
        return out

    def predict_mask(self, batch):
        logit_mask = self(batch['query_img'], batch['support_imgs'], batch['support_masks'])
        org_qry_imsize = tuple([batch['org_query_imsize'][1].item(), batch['org_query_imsize'][0].item()])
        logit_mask = F.interpolate(logit_mask, org_qry_imsize, mode='bilinear', align_corners=True)
        return logit_mask

    def compute_loss(self, logit_mask, gt_mask):
        return self.criterion(logit_mask, gt_mask.long())

    def generate_prior(self, query_feat_high, supp_feat_high, s_y, fts_size):
        bsize, _, sp_sz, _ = query_feat_high.size()[:]
        corr_query_mask_list = []
        cosine_eps = 1e-7
        for st in range(self.shot):
            tmp_mask = (s_y[:, st, :, :] == 1).float().unsqueeze(1)
            tmp_mask = F.interpolate(tmp_mask, size=(fts_size[0], fts_size[1]), mode='bilinear', align_corners=True)

            tmp_supp_feat = supp_feat_high[:, st, ...] * tmp_mask
            q = self.high_avg_pool(query_feat_high.flatten(2).transpose(-2, -1))  # [bs, h*w, c]
            s = self.high_avg_pool(tmp_supp_feat.flatten(2).transpose(-2, -1))  # [bs, h*w, c]

            tmp_query = q
            tmp_query = tmp_query.contiguous().permute(0, 2, 1)  # [bs, c, h*w]
            tmp_query_norm = torch.norm(tmp_query, 2, 1, True)

            tmp_supp = s
            tmp_supp = tmp_supp.contiguous()
            tmp_supp = tmp_supp.contiguous()
            tmp_supp_norm = torch.norm(tmp_supp, 2, 2, True)

            similarity = torch.bmm(tmp_supp, tmp_query) / (torch.bmm(tmp_supp_norm, tmp_query_norm) + cosine_eps)
            similarity = similarity.max(1)[0].view(bsize, sp_sz * sp_sz)
            similarity = (similarity - similarity.min(1)[0].unsqueeze(1)) / (
                        similarity.max(1)[0].unsqueeze(1) - similarity.min(1)[0].unsqueeze(1) + cosine_eps)
            corr_query = similarity.view(bsize, 1, sp_sz, sp_sz)
            corr_query = F.interpolate(corr_query, size=(fts_size[0], fts_size[1]), mode='bilinear', align_corners=True)
            corr_query_mask_list.append(corr_query)
        corr_query_mask = torch.cat(corr_query_mask_list, 1).mean(1).unsqueeze(1)
        return corr_query_mask

    # def mask_feature(self, features, support_mask):#bchw
    #     bs=24#64
    #     # initSize=((features[0].shape[-1])*2,)*2
    #     support_mask = (support_mask).float()
    #     #features 24 64 60 60
    #     #support_mask 24 1 60 60
    #     # support_mask = F.interpolate(support_mask, initSize, mode='bilinear', align_corners=True)
    #     for idx, feature in enumerate(features):
    #         feat=[]
    #         #feature 64 60 60
    #         # if support_mask.shape[-1]!=feature.shape[-1]:
    #         #     support_mask = F.interpolate(support_mask, feature.size()[2:], mode='bilinear', align_corners=True)
    #         for i in range(bs):
    #             featI=features[i].flatten(1)#c,hw 64 3600
    #
    #             maskI=support_mask[i].flatten(1)#hw1,60,60-->1,3600
    #             featI = featI * maskI #64 3600
    #             maskI=maskI.squeeze()#3600
    #             meanVal=maskI[maskI>0].mean()
    #             realSupI=featI[:,maskI>=meanVal]
    #             if maskI.sum()==0:
    #                 # realSupI=torch.zeros(featI.shape[0],1).cuda()
    #                 realSupI = torch.cuda.FloatTensor(torch.zeros(featI.shape[0],1))
    #             feat.append(realSupI)#[b,]ch,w
    #         print(feat)
    #         features[idx] = feat#nfeatures ,bs,ch,w
    #     return features