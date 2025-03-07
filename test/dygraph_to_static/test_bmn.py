# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import os
import tempfile
import unittest

import numpy as np
from dygraph_to_static_utils import (
    Dy2StTestBase,
    enable_to_static_guard,
    static_guard,
    test_legacy_and_pt_and_pir,
)
from predictor_utils import PredictorTools

import paddle
from paddle import base
from paddle.base import ParamAttr
from paddle.base.dygraph import to_variable
from paddle.base.framework import unique_name
from paddle.jit.translated_layer import INFER_MODEL_SUFFIX, INFER_PARAMS_SUFFIX

SEED = 2000
DATATYPE = 'float32'

# Note: Set True to eliminate randomness.
#     1. For one operation, cuDNN has several algorithms,
#        some algorithm results are non-deterministic, like convolution algorithms.
if paddle.is_compiled_with_cuda():
    paddle.set_flags({'FLAGS_cudnn_deterministic': True})


def get_interp1d_mask(
    tscale, dscale, prop_boundary_ratio, num_sample, num_sample_perbin
):
    """generate sample mask for each point in Boundary-Matching Map"""
    mask_mat = []
    for start_index in range(tscale):
        mask_mat_vector = []
        for duration_index in range(dscale):
            if start_index + duration_index < tscale:
                p_xmin = start_index
                p_xmax = start_index + duration_index
                center_len = float(p_xmax - p_xmin) + 1
                sample_xmin = p_xmin - center_len * prop_boundary_ratio
                sample_xmax = p_xmax + center_len * prop_boundary_ratio
                p_mask = _get_interp1d_bin_mask(
                    sample_xmin,
                    sample_xmax,
                    tscale,
                    num_sample,
                    num_sample_perbin,
                )
            else:
                p_mask = np.zeros([tscale, num_sample])
            mask_mat_vector.append(p_mask)
        mask_mat_vector = np.stack(mask_mat_vector, axis=2)
        mask_mat.append(mask_mat_vector)
    mask_mat = np.stack(mask_mat, axis=3)
    mask_mat = mask_mat.astype(np.float32)

    sample_mask = np.reshape(mask_mat, [tscale, -1])
    return sample_mask


def _get_interp1d_bin_mask(
    seg_xmin, seg_xmax, tscale, num_sample, num_sample_perbin
):
    """generate sample mask for a boundary-matching pair"""
    plen = float(seg_xmax - seg_xmin)
    plen_sample = plen / (num_sample * num_sample_perbin - 1.0)
    total_samples = [
        seg_xmin + plen_sample * ii
        for ii in range(num_sample * num_sample_perbin)
    ]
    p_mask = []
    for idx in range(num_sample):
        bin_samples = total_samples[
            idx * num_sample_perbin : (idx + 1) * num_sample_perbin
        ]
        bin_vector = np.zeros([tscale])
        for sample in bin_samples:
            sample_upper = math.ceil(sample)
            sample_decimal, sample_down = math.modf(sample)
            if int(sample_down) <= (tscale - 1) and int(sample_down) >= 0:
                bin_vector[int(sample_down)] += 1 - sample_decimal
            if int(sample_upper) <= (tscale - 1) and int(sample_upper) >= 0:
                bin_vector[int(sample_upper)] += sample_decimal
        bin_vector = 1.0 / num_sample_perbin * bin_vector
        p_mask.append(bin_vector)
    p_mask = np.stack(p_mask, axis=1)
    return p_mask


class Conv1D(paddle.nn.Layer):
    def __init__(
        self,
        prefix,
        num_channels=256,
        num_filters=256,
        size_k=3,
        padding=1,
        groups=1,
        act="relu",
    ):
        super().__init__()
        fan_in = num_channels * size_k * 1
        k = 1.0 / math.sqrt(fan_in)
        param_attr = ParamAttr(
            name=prefix + "_w",
            initializer=paddle.nn.initializer.Uniform(low=-k, high=k),
        )
        bias_attr = ParamAttr(
            name=prefix + "_b",
            initializer=paddle.nn.initializer.Uniform(low=-k, high=k),
        )

        self._conv2d = paddle.nn.Conv2D(
            in_channels=num_channels,
            out_channels=num_filters,
            kernel_size=(1, size_k),
            stride=1,
            padding=(0, padding),
            groups=groups,
            weight_attr=param_attr,
            bias_attr=bias_attr,
        )

    def forward(self, x):
        x = paddle.unsqueeze(x, axis=[2])
        x = self._conv2d(x)
        x = paddle.squeeze(x, axis=[2])
        return x


class BMN(paddle.nn.Layer):
    def __init__(self, cfg):
        super().__init__()

        self.tscale = cfg.tscale
        self.dscale = cfg.dscale
        self.prop_boundary_ratio = cfg.prop_boundary_ratio
        self.num_sample = cfg.num_sample
        self.num_sample_perbin = cfg.num_sample_perbin

        self.hidden_dim_1d = 256
        self.hidden_dim_2d = 128
        self.hidden_dim_3d = 512

        # Base Module
        self.b_conv1 = Conv1D(
            prefix="Base_1",
            num_channels=cfg.feat_dim,
            num_filters=self.hidden_dim_1d,
            size_k=3,
            padding=1,
            groups=4,
            act="relu",
        )
        self.b_conv2 = Conv1D(
            prefix="Base_2",
            num_filters=self.hidden_dim_1d,
            size_k=3,
            padding=1,
            groups=4,
            act="relu",
        )

        # Temporal Evaluation Module
        self.ts_conv1 = Conv1D(
            prefix="TEM_s1",
            num_filters=self.hidden_dim_1d,
            size_k=3,
            padding=1,
            groups=4,
            act="relu",
        )
        self.ts_conv2 = Conv1D(
            prefix="TEM_s2", num_filters=1, size_k=1, padding=0, act="sigmoid"
        )
        self.te_conv1 = Conv1D(
            prefix="TEM_e1",
            num_filters=self.hidden_dim_1d,
            size_k=3,
            padding=1,
            groups=4,
            act="relu",
        )
        self.te_conv2 = Conv1D(
            prefix="TEM_e2", num_filters=1, size_k=1, padding=0, act="sigmoid"
        )

        # Proposal Evaluation Module
        self.p_conv1 = Conv1D(
            prefix="PEM_1d",
            num_filters=self.hidden_dim_2d,
            size_k=3,
            padding=1,
            act="relu",
        )

        # init to speed up
        sample_mask = get_interp1d_mask(
            self.tscale,
            self.dscale,
            self.prop_boundary_ratio,
            self.num_sample,
            self.num_sample_perbin,
        )
        self.sample_mask = base.dygraph.base.to_variable(sample_mask)
        self.sample_mask.stop_gradient = True

        self.p_conv3d1 = paddle.nn.Conv3D(
            in_channels=128,
            out_channels=self.hidden_dim_3d,
            kernel_size=(self.num_sample, 1, 1),
            stride=(self.num_sample, 1, 1),
            padding=0,
            weight_attr=paddle.ParamAttr(name="PEM_3d1_w"),
            bias_attr=paddle.ParamAttr(name="PEM_3d1_b"),
        )

        self.p_conv2d1 = paddle.nn.Conv2D(
            in_channels=512,
            out_channels=self.hidden_dim_2d,
            kernel_size=1,
            stride=1,
            padding=0,
            weight_attr=ParamAttr(name="PEM_2d1_w"),
            bias_attr=ParamAttr(name="PEM_2d1_b"),
        )
        self.p_conv2d2 = paddle.nn.Conv2D(
            in_channels=128,
            out_channels=self.hidden_dim_2d,
            kernel_size=3,
            stride=1,
            padding=1,
            weight_attr=ParamAttr(name="PEM_2d2_w"),
            bias_attr=ParamAttr(name="PEM_2d2_b"),
        )
        self.p_conv2d3 = paddle.nn.Conv2D(
            in_channels=128,
            out_channels=self.hidden_dim_2d,
            kernel_size=3,
            stride=1,
            padding=1,
            weight_attr=ParamAttr(name="PEM_2d3_w"),
            bias_attr=ParamAttr(name="PEM_2d3_b"),
        )
        self.p_conv2d4 = paddle.nn.Conv2D(
            in_channels=128,
            out_channels=2,
            kernel_size=1,
            stride=1,
            padding=0,
            weight_attr=ParamAttr(name="PEM_2d4_w"),
            bias_attr=ParamAttr(name="PEM_2d4_b"),
        )

    def forward(self, x):
        # Base Module
        x = paddle.nn.functional.relu(self.b_conv1(x))
        x = paddle.nn.functional.relu(self.b_conv2(x))

        # TEM
        xs = paddle.nn.functional.relu(self.ts_conv1(x))
        xs = paddle.nn.functional.relu(self.ts_conv2(xs))
        xs = paddle.squeeze(xs, axis=[1])
        xe = paddle.nn.functional.relu(self.te_conv1(x))
        xe = paddle.nn.functional.relu(self.te_conv2(xe))
        xe = paddle.squeeze(xe, axis=[1])

        # PEM
        xp = paddle.nn.functional.relu(self.p_conv1(x))
        # BM layer
        xp = paddle.matmul(xp, self.sample_mask)
        xp = paddle.reshape(xp, shape=[0, 0, -1, self.dscale, self.tscale])

        xp = self.p_conv3d1(xp)
        xp = paddle.tanh(xp)
        xp = paddle.squeeze(xp, axis=[2])
        xp = paddle.nn.functional.relu(self.p_conv2d1(xp))
        xp = paddle.nn.functional.relu(self.p_conv2d2(xp))
        xp = paddle.nn.functional.relu(self.p_conv2d3(xp))
        xp = paddle.nn.functional.sigmoid(self.p_conv2d4(xp))
        return xp, xs, xe


def bmn_loss_func(
    pred_bm, pred_start, pred_end, gt_iou_map, gt_start, gt_end, cfg
):
    def _get_mask(cfg):
        dscale = cfg.dscale
        tscale = cfg.tscale
        bm_mask = []
        for idx in range(dscale):
            mask_vector = [1 for i in range(tscale - idx)] + [
                0 for i in range(idx)
            ]
            bm_mask.append(mask_vector)
        bm_mask = np.array(bm_mask, dtype=np.float32)
        self_bm_mask = paddle.static.create_global_var(
            shape=[dscale, tscale], value=0, dtype=DATATYPE, persistable=True
        )
        paddle.assign(bm_mask, self_bm_mask)
        self_bm_mask.stop_gradient = True
        return self_bm_mask

    def tem_loss_func(pred_start, pred_end, gt_start, gt_end):
        def bi_loss(pred_score, gt_label):
            pred_score = paddle.reshape(x=pred_score, shape=[-1])
            gt_label = paddle.reshape(x=gt_label, shape=[-1])
            gt_label.stop_gradient = True
            pmask = paddle.cast(x=(gt_label > 0.5), dtype=DATATYPE)
            num_entries = paddle.cast(paddle.shape(pmask), dtype=DATATYPE)
            num_positive = paddle.cast(paddle.sum(pmask), dtype=DATATYPE)
            ratio = num_entries / num_positive
            coef_0 = 0.5 * ratio / (ratio - 1)
            coef_1 = 0.5 * ratio
            epsilon = 0.000001
            # temp = paddle.log(pred_score + epsilon)
            loss_pos = paddle.multiply(paddle.log(pred_score + epsilon), pmask)
            loss_pos = coef_1 * paddle.mean(loss_pos)
            loss_neg = paddle.multiply(
                paddle.log(1.0 - pred_score + epsilon), (1.0 - pmask)
            )
            loss_neg = coef_0 * paddle.mean(loss_neg)
            loss = -1 * (loss_pos + loss_neg)
            return loss

        loss_start = bi_loss(pred_start, gt_start)
        loss_end = bi_loss(pred_end, gt_end)
        loss = loss_start + loss_end
        return loss

    def pem_reg_loss_func(pred_score, gt_iou_map, mask):
        gt_iou_map = paddle.multiply(gt_iou_map, mask)

        u_hmask = paddle.cast(x=gt_iou_map > 0.7, dtype=DATATYPE)
        u_mmask = paddle.logical_and(gt_iou_map <= 0.7, gt_iou_map > 0.3)
        u_mmask = paddle.cast(x=u_mmask, dtype=DATATYPE)
        u_lmask = paddle.logical_and(gt_iou_map <= 0.3, gt_iou_map >= 0.0)
        u_lmask = paddle.cast(x=u_lmask, dtype=DATATYPE)
        u_lmask = paddle.multiply(u_lmask, mask)

        num_h = paddle.cast(paddle.sum(u_hmask), dtype=DATATYPE)
        num_m = paddle.cast(paddle.sum(u_mmask), dtype=DATATYPE)
        num_l = paddle.cast(paddle.sum(u_lmask), dtype=DATATYPE)

        r_m = num_h / num_m
        u_smmask = paddle.assign(
            local_random.uniform(
                0.0, 1.0, [gt_iou_map.shape[1], gt_iou_map.shape[2]]
            ).astype(DATATYPE)
        )
        u_smmask = paddle.multiply(u_mmask, u_smmask)
        u_smmask = paddle.cast(x=(u_smmask > (1.0 - r_m)), dtype=DATATYPE)

        r_l = num_h / num_l
        u_slmask = paddle.assign(
            local_random.uniform(
                0.0, 1.0, [gt_iou_map.shape[1], gt_iou_map.shape[2]]
            ).astype(DATATYPE)
        )
        u_slmask = paddle.multiply(u_lmask, u_slmask)
        u_slmask = paddle.cast(x=(u_slmask > (1.0 - r_l)), dtype=DATATYPE)

        weights = u_hmask + u_smmask + u_slmask
        weights.stop_gradient = True
        loss = paddle.nn.functional.square_error_cost(pred_score, gt_iou_map)
        loss = paddle.multiply(loss, weights)
        loss = 0.5 * paddle.sum(loss) / paddle.sum(weights)

        return loss

    def pem_cls_loss_func(pred_score, gt_iou_map, mask):
        gt_iou_map = paddle.multiply(gt_iou_map, mask)
        gt_iou_map.stop_gradient = True
        pmask = paddle.cast(x=(gt_iou_map > 0.9), dtype=DATATYPE)
        nmask = paddle.cast(x=(gt_iou_map <= 0.9), dtype=DATATYPE)
        nmask = paddle.multiply(nmask, mask)

        num_positive = paddle.sum(pmask)
        num_entries = num_positive + paddle.sum(nmask)
        ratio = num_entries / num_positive
        coef_0 = 0.5 * ratio / (ratio - 1)
        coef_1 = 0.5 * ratio
        epsilon = 0.000001
        loss_pos = paddle.multiply(paddle.log(pred_score + epsilon), pmask)
        loss_pos = coef_1 * paddle.sum(loss_pos)
        loss_neg = paddle.multiply(
            paddle.log(1.0 - pred_score + epsilon), nmask
        )
        loss_neg = coef_0 * paddle.sum(loss_neg)
        loss = -1 * (loss_pos + loss_neg) / num_entries
        return loss

    pred_bm_reg = paddle.squeeze(
        paddle.slice(pred_bm, axes=[1], starts=[0], ends=[1]), axis=[1]
    )
    pred_bm_cls = paddle.squeeze(
        paddle.slice(pred_bm, axes=[1], starts=[1], ends=[2]), axis=[1]
    )

    bm_mask = _get_mask(cfg)

    pem_reg_loss = pem_reg_loss_func(pred_bm_reg, gt_iou_map, bm_mask)
    pem_cls_loss = pem_cls_loss_func(pred_bm_cls, gt_iou_map, bm_mask)

    tem_loss = tem_loss_func(pred_start, pred_end, gt_start, gt_end)

    loss = tem_loss + 10 * pem_reg_loss + pem_cls_loss
    return loss, tem_loss, pem_reg_loss, pem_cls_loss


class Args:
    epoch = 1
    batch_size = 4
    learning_rate = 0.1
    learning_rate_decay = 0.1
    lr_decay_iter = 4200
    l2_weight_decay = 1e-4
    valid_interval = 20
    log_interval = 5
    train_batch_num = valid_interval
    valid_batch_num = 5

    tscale = 50
    dscale = 50
    feat_dim = 100
    prop_boundary_ratio = 0.5
    num_sample = 2
    num_sample_perbin = 2


def optimizer(cfg, parameter_list):
    bd = [cfg.lr_decay_iter]
    base_lr = cfg.learning_rate
    lr_decay = cfg.learning_rate_decay
    l2_weight_decay = cfg.l2_weight_decay
    lr = [base_lr, base_lr * lr_decay]
    optimizer = paddle.optimizer.Adam(
        paddle.optimizer.lr.PiecewiseDecay(boundaries=bd, values=lr),
        parameters=parameter_list,
        weight_decay=paddle.regularizer.L2Decay(coeff=l2_weight_decay),
    )
    return optimizer


def fake_data_reader(args, mode='train'):
    def iou_with_anchors(anchors_min, anchors_max, box_min, box_max):
        """Compute jaccard score between a box and the anchors."""
        len_anchors = anchors_max - anchors_min
        int_xmin = np.maximum(anchors_min, box_min)
        int_xmax = np.minimum(anchors_max, box_max)
        inter_len = np.maximum(int_xmax - int_xmin, 0.0)
        union_len = len_anchors - inter_len + box_max - box_min
        jaccard = np.divide(inter_len, union_len)
        return jaccard

    def ioa_with_anchors(anchors_min, anchors_max, box_min, box_max):
        """Compute intersection between score a box and the anchors."""
        len_anchors = anchors_max - anchors_min
        int_xmin = np.maximum(anchors_min, box_min)
        int_xmax = np.minimum(anchors_max, box_max)
        inter_len = np.maximum(int_xmax - int_xmin, 0.0)
        scores = np.divide(inter_len, len_anchors)
        return scores

    def get_match_map(tscale):
        match_map = []
        tgap = 1.0 / tscale
        for idx in range(tscale):
            tmp_match_window = []
            xmin = tgap * idx
            for jdx in range(1, tscale + 1):
                xmax = xmin + tgap * jdx
                tmp_match_window.append([xmin, xmax])
            match_map.append(tmp_match_window)
        match_map = np.array(match_map)
        match_map = np.transpose(match_map, [1, 0, 2])
        match_map = np.reshape(match_map, [-1, 2])
        match_map = match_map
        anchor_xmin = [tgap * i for i in range(tscale)]
        anchor_xmax = [tgap * i for i in range(1, tscale + 1)]

        return match_map, anchor_xmin, anchor_xmax

    def get_video_label(match_map, anchor_xmin, anchor_xmax):
        video_second = local_random.randint(75, 90)
        label_num = local_random.randint(1, 3)

        gt_bbox = []
        gt_iou_map = []
        for idx in range(label_num):
            duration = local_random.uniform(
                video_second * 0.4, video_second * 0.8
            )
            start_t = local_random.uniform(
                0.1 * video_second, video_second - duration
            )
            tmp_start = max(min(1, start_t / video_second), 0)
            tmp_end = max(min(1, (start_t + duration) / video_second), 0)
            gt_bbox.append([tmp_start, tmp_end])
            tmp_gt_iou_map = iou_with_anchors(
                match_map[:, 0], match_map[:, 1], tmp_start, tmp_end
            )
            tmp_gt_iou_map = np.reshape(
                tmp_gt_iou_map, [args.dscale, args.tscale]
            )
            gt_iou_map.append(tmp_gt_iou_map)
        gt_iou_map = np.array(gt_iou_map)
        gt_iou_map = np.max(gt_iou_map, axis=0)

        gt_bbox = np.array(gt_bbox)
        gt_xmins = gt_bbox[:, 0]
        gt_xmaxs = gt_bbox[:, 1]
        gt_len_small = 3.0 / args.tscale
        gt_start_bboxs = np.stack(
            (gt_xmins - gt_len_small / 2, gt_xmins + gt_len_small / 2), axis=1
        )
        gt_end_bboxs = np.stack(
            (gt_xmaxs - gt_len_small / 2, gt_xmaxs + gt_len_small / 2), axis=1
        )

        match_score_start = []
        for jdx in range(len(anchor_xmin)):
            match_score_start.append(
                np.max(
                    ioa_with_anchors(
                        anchor_xmin[jdx],
                        anchor_xmax[jdx],
                        gt_start_bboxs[:, 0],
                        gt_start_bboxs[:, 1],
                    )
                )
            )
        match_score_end = []
        for jdx in range(len(anchor_xmin)):
            match_score_end.append(
                np.max(
                    ioa_with_anchors(
                        anchor_xmin[jdx],
                        anchor_xmax[jdx],
                        gt_end_bboxs[:, 0],
                        gt_end_bboxs[:, 1],
                    )
                )
            )

        gt_start = np.array(match_score_start)
        gt_end = np.array(match_score_end)
        return gt_iou_map, gt_start, gt_end

    def reader():
        batch_out = []
        iter_num = args.batch_size * 100
        match_map, anchor_xmin, anchor_xmax = get_match_map(args.tscale)

        for video_idx in range(iter_num):
            video_feat = local_random.random_sample(
                [args.feat_dim, args.tscale]
            ).astype('float32')
            gt_iou_map, gt_start, gt_end = get_video_label(
                match_map, anchor_xmin, anchor_xmax
            )

            if mode == 'train' or mode == 'valid':
                batch_out.append((video_feat, gt_iou_map, gt_start, gt_end))
            elif mode == 'test':
                batch_out.append(
                    (video_feat, gt_iou_map, gt_start, gt_end, video_idx)
                )
            else:
                raise NotImplementedError(f'mode {mode} not implemented')
            if len(batch_out) == args.batch_size:
                yield batch_out
                batch_out = []

    return reader


# Validation
def val_bmn(model, args):
    val_reader = fake_data_reader(args, 'valid')

    loss_data = []
    for batch_id, data in enumerate(val_reader()):
        video_feat = np.array([item[0] for item in data]).astype(DATATYPE)
        gt_iou_map = np.array([item[1] for item in data]).astype(DATATYPE)
        gt_start = np.array([item[2] for item in data]).astype(DATATYPE)
        gt_end = np.array([item[3] for item in data]).astype(DATATYPE)

        x_data = to_variable(video_feat)
        gt_iou_map = to_variable(gt_iou_map)
        gt_start = to_variable(gt_start)
        gt_end = to_variable(gt_end)
        gt_iou_map.stop_gradient = True
        gt_start.stop_gradient = True
        gt_end.stop_gradient = True

        pred_bm, pred_start, pred_end = model(x_data)

        loss, tem_loss, pem_reg_loss, pem_cls_loss = bmn_loss_func(
            pred_bm, pred_start, pred_end, gt_iou_map, gt_start, gt_end, args
        )
        avg_loss = paddle.mean(loss)

        loss_data += [
            float(avg_loss),
            float(tem_loss),
            float(pem_reg_loss),
            float(pem_cls_loss),
        ]

        if batch_id == args.valid_batch_num:
            break
    return loss_data


class TestTrain(Dy2StTestBase):
    def setUp(self):
        self.args = Args()
        self.place = (
            paddle.CPUPlace()
            if not paddle.is_compiled_with_cuda()
            else paddle.CUDAPlace(0)
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.model_save_dir = os.path.join(self.temp_dir.name, 'inference')
        self.model_save_prefix = os.path.join(self.model_save_dir, 'bmn')
        self.model_filename = "bmn" + INFER_MODEL_SUFFIX
        self.params_filename = "bmn" + INFER_PARAMS_SUFFIX
        self.dy_param_path = os.path.join(self.temp_dir.name, 'bmn_dy_param')

    def tearDown(self):
        self.temp_dir.cleanup()

    def train_bmn(self, args, to_static):
        with unique_name.guard():
            loss_data = []

            paddle.seed(SEED)
            paddle.framework.random._manual_program_seed(SEED)
            global local_random
            local_random = np.random.RandomState(SEED)

            bmn = paddle.jit.to_static(BMN(args))
            adam = optimizer(args, parameter_list=bmn.parameters())

            train_reader = fake_data_reader(args, 'train')

            for epoch in range(args.epoch):
                for batch_id, data in enumerate(train_reader()):
                    video_feat = np.array([item[0] for item in data]).astype(
                        DATATYPE
                    )
                    gt_iou_map = np.array([item[1] for item in data]).astype(
                        DATATYPE
                    )
                    gt_start = np.array([item[2] for item in data]).astype(
                        DATATYPE
                    )
                    gt_end = np.array([item[3] for item in data]).astype(
                        DATATYPE
                    )

                    x_data = to_variable(video_feat)
                    gt_iou_map = to_variable(gt_iou_map)
                    gt_start = to_variable(gt_start)
                    gt_end = to_variable(gt_end)
                    gt_iou_map.stop_gradient = True
                    gt_start.stop_gradient = True
                    gt_end.stop_gradient = True

                    pred_bm, pred_start, pred_end = bmn(x_data)

                    loss, tem_loss, pem_reg_loss, pem_cls_loss = bmn_loss_func(
                        pred_bm,
                        pred_start,
                        pred_end,
                        gt_iou_map,
                        gt_start,
                        gt_end,
                        args,
                    )
                    avg_loss = paddle.mean(loss)

                    avg_loss.backward()
                    adam.minimize(avg_loss)
                    bmn.clear_gradients()
                    # log loss data to verify correctness
                    loss_data += [
                        float(avg_loss),
                        float(tem_loss),
                        float(pem_reg_loss),
                        float(pem_cls_loss),
                    ]

                    # validation
                    if batch_id % args.valid_interval == 0 and batch_id > 0:
                        bmn.eval()
                        val_loss_data = val_bmn(bmn, args)
                        bmn.train()
                        loss_data += val_loss_data

                    if batch_id == args.train_batch_num:
                        # TODO(@xiongkun): open after save / load supported in pir.
                        if (
                            to_static
                            and not paddle.base.framework.use_pir_api()
                        ):
                            paddle.jit.save(bmn, self.model_save_prefix)
                        else:
                            paddle.save(
                                bmn.state_dict(),
                                self.dy_param_path + '.pdparams',
                            )
                        break
            return np.array(loss_data)

    @test_legacy_and_pt_and_pir
    def test_train_pir(self):
        with enable_to_static_guard(True):
            static_res = self.train_bmn(self.args, to_static=True)
        with enable_to_static_guard(False):
            dygraph_res = self.train_bmn(self.args, to_static=False)
        np.testing.assert_allclose(
            dygraph_res,
            static_res,
            rtol=1e-05,
            err_msg='dygraph_res: {},\n static_res: {}'.format(
                dygraph_res[~np.isclose(dygraph_res, static_res)],
                static_res[~np.isclose(dygraph_res, static_res)],
            ),
            atol=1e-8,
        )

    def test_train(self):
        with enable_to_static_guard(True):
            static_res = self.train_bmn(self.args, to_static=True)
        with enable_to_static_guard(False):
            dygraph_res = self.train_bmn(self.args, to_static=False)
        np.testing.assert_allclose(
            dygraph_res,
            static_res,
            rtol=1e-05,
            err_msg='dygraph_res: {},\n static_res: {}'.format(
                dygraph_res[~np.isclose(dygraph_res, static_res)],
                static_res[~np.isclose(dygraph_res, static_res)],
            ),
            atol=1e-8,
        )

        # Prediction needs trained models, so put `test_predict` at last of `test_train`
        self.verify_predict()

    def verify_predict(self):
        args = Args()
        args.batch_size = 1  # change batch_size
        test_reader = fake_data_reader(args, 'test')
        for batch_id, data in enumerate(test_reader()):
            video_data = np.array([item[0] for item in data]).astype(DATATYPE)
            static_pred_res = self.predict_static(video_data)
            dygraph_pred_res = self.predict_dygraph(video_data)
            dygraph_jit_pred_res = self.predict_dygraph_jit(video_data)
            predictor_pred_res = self.predict_analysis_inference(video_data)

            for dy_res, st_res, dy_jit_res, predictor_res in zip(
                dygraph_pred_res,
                static_pred_res,
                dygraph_jit_pred_res,
                predictor_pred_res,
            ):
                np.testing.assert_allclose(
                    st_res,
                    dy_res,
                    rtol=1e-05,
                    err_msg='dygraph_res: {},\n static_res: {}'.format(
                        dy_res[~np.isclose(st_res, dy_res)],
                        st_res[~np.isclose(st_res, dy_res)],
                    ),
                    atol=1e-8,
                )
                np.testing.assert_allclose(
                    st_res,
                    dy_jit_res,
                    rtol=1e-05,
                    err_msg='dygraph_jit_res: {},\n static_res: {}'.format(
                        dy_jit_res[~np.isclose(st_res, dy_jit_res)],
                        st_res[~np.isclose(st_res, dy_jit_res)],
                    ),
                    atol=1e-8,
                )
                np.testing.assert_allclose(
                    st_res,
                    predictor_res,
                    rtol=1e-05,
                    err_msg='dygraph_jit_res: {},\n static_res: {}'.format(
                        predictor_res[~np.isclose(st_res, predictor_res)],
                        st_res[~np.isclose(st_res, predictor_res)],
                    ),
                    atol=1e-8,
                )
            break

    def predict_dygraph(self, data):
        with enable_to_static_guard(False):
            bmn = paddle.jit.to_static(BMN(self.args))
            # load dygraph trained parameters
            model_dict = paddle.load(self.dy_param_path + ".pdparams")
            bmn.set_dict(model_dict)
            bmn.eval()

            x = to_variable(data)
            pred_res = bmn(x)
            pred_res = [var.numpy() for var in pred_res]

        return pred_res

    def predict_static(self, data):
        with static_guard():
            exe = paddle.static.Executor(self.place)
            # load inference model
            [
                inference_program,
                feed_target_names,
                fetch_targets,
            ] = paddle.static.io.load_inference_model(
                self.model_save_dir,
                executor=exe,
                model_filename=self.model_filename,
                params_filename=self.params_filename,
            )
            pred_res = exe.run(
                inference_program,
                feed={feed_target_names[0]: data},
                fetch_list=fetch_targets,
            )
        return pred_res

    def predict_dygraph_jit(self, data):
        bmn = paddle.jit.load(self.model_save_prefix)
        bmn.eval()

        x = to_variable(data)
        pred_res = bmn(x)
        pred_res = [var.numpy() for var in pred_res]

        return pred_res

    def predict_analysis_inference(self, data):
        output = PredictorTools(
            self.model_save_dir,
            self.model_filename,
            self.params_filename,
            [data],
        )
        out = output()
        return out


if __name__ == "__main__":
    unittest.main()
