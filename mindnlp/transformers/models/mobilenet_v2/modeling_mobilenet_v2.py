# Copyright 2024 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================
"""Mindspore MobileNetV2 model."""

from typing import Optional, Union

import mindspore as ms
from mindspore.common.initializer import initializer, Normal

from mindnlp.core import nn, ops
from mindnlp.core.nn import functional as F
from ....common.activations import ACT2FN
from ...modeling_outputs import (
    BaseModelOutputWithPoolingAndNoAttention,
    ImageClassifierOutputWithNoAttention,
    SemanticSegmenterOutput,
)
from ...modeling_utils import PreTrainedModel
from ....utils import logging
from .configuration_mobilenet_v2 import MobileNetV2Config


logger = logging.get_logger(__name__)


def make_divisible(value: int, divisor: int = 8, min_value: Optional[int] = None) -> int:
    """
    Ensure that all layers have a channel count that is divisible by `divisor`. This function is taken from the
    original TensorFlow repo. It can be seen here:
    https://github.com/tensorflow/models/blob/master/research/slim/nets/mobilenet/mobilenet.py
    """
    if min_value is None:
        min_value = divisor
    new_value = max(min_value, int(value + divisor / 2) // divisor * divisor)
    # Make sure that round down does not go down by more than 10%.
    if new_value < 0.9 * value:
        new_value += divisor
    return int(new_value)


def apply_depth_multiplier(config: MobileNetV2Config, channels: int) -> int:
    return make_divisible(int(round(channels * config.depth_multiplier)), config.depth_divisible_by, config.min_depth)


def apply_tf_padding(features: ms.Tensor, conv_layer: nn.Conv2d) -> ms.Tensor:
    """
    Apply TensorFlow-style "SAME" padding to a convolution layer. See the notes at:
    https://www.tensorflow.org/api_docs/python/tf/nn#notes_on_padding_2
    """
    in_height = int(features.shape[-2])
    in_width = int(features.shape[-1])
    stride_height, stride_width = conv_layer.stride
    kernel_height, kernel_width = conv_layer.kernel_size
    dilation_height, dilation_width = conv_layer.dilation

    if in_height % stride_height == 0:
        pad_along_height = max(kernel_height - stride_height, 0)
    else:
        pad_along_height = max(kernel_height - (in_height % stride_height), 0)

    if in_width % stride_width == 0:
        pad_along_width = max(kernel_width - stride_width, 0)
    else:
        pad_along_width = max(kernel_width - (in_width % stride_width), 0)

    pad_left = pad_along_width // 2
    pad_right = pad_along_width - pad_left
    pad_top = pad_along_height // 2
    pad_bottom = pad_along_height - pad_top

    padding = (
        pad_left * dilation_width,
        pad_right * dilation_width,
        pad_top * dilation_height,
        pad_bottom * dilation_height,
    )
    return ops.pad(features, padding, "constant", 0.0)


class MobileNetV2ConvLayer(nn.Module):
    def __init__(
        self,
        config: MobileNetV2Config,
        in_channels: int,
        out_channels: int,
        kernel_size: int,
        stride: int = 1,
        groups: int = 1,
        bias: bool = False,
        dilation: int = 1,
        use_normalization: bool = True,
        use_activation: Union[bool, str] = True,
        layer_norm_eps: Optional[float] = None,
    ) -> None:
        super().__init__()
        self.config = config

        if in_channels % groups != 0:
            raise ValueError(f"Input channels ({in_channels}) are not divisible by {groups} groups.")
        if out_channels % groups != 0:
            raise ValueError(f"Output channels ({out_channels}) are not divisible by {groups} groups.")

        padding = 0 if config.tf_padding else int((kernel_size - 1) / 2) * dilation

        self.convolution = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=bias,
        )

        if use_normalization:
            self.normalization = nn.BatchNorm2d(
                num_features=out_channels,
                eps=config.layer_norm_eps if layer_norm_eps is None else layer_norm_eps,
                momentum=0.997,
                affine=True,
                track_running_stats=True,
            )
        else:
            self.normalization = None

        if use_activation:
            if isinstance(use_activation, str):
                self.activation = ACT2FN[use_activation]
            elif isinstance(config.hidden_act, str):
                self.activation = ACT2FN[config.hidden_act]
            else:
                self.activation = config.hidden_act
        else:
            self.activation = None

    def forward(self, features: ms.Tensor) -> ms.Tensor:
        if self.config.tf_padding:
            features = apply_tf_padding(features, self.convolution)
        features = self.convolution(features)
        if self.normalization is not None:
            features = self.normalization(features)
        if self.activation is not None:
            features = self.activation(features)
        return features


class MobileNetV2InvertedResidual(nn.Module):
    def __init__(
        self, config: MobileNetV2Config, in_channels: int, out_channels: int, stride: int, dilation: int = 1
    ) -> None:
        super().__init__()

        expanded_channels = make_divisible(
            int(round(in_channels * config.expand_ratio)), config.depth_divisible_by, config.min_depth
        )

        if stride not in [1, 2]:
            raise ValueError(f"Invalid stride {stride}.")

        self.use_residual = (stride == 1) and (in_channels == out_channels)

        self.expand_1x1 = MobileNetV2ConvLayer(
            config, in_channels=in_channels, out_channels=expanded_channels, kernel_size=1
        )

        self.conv_3x3 = MobileNetV2ConvLayer(
            config,
            in_channels=expanded_channels,
            out_channels=expanded_channels,
            kernel_size=3,
            stride=stride,
            groups=expanded_channels,
            dilation=dilation,
        )

        self.reduce_1x1 = MobileNetV2ConvLayer(
            config,
            in_channels=expanded_channels,
            out_channels=out_channels,
            kernel_size=1,
            use_activation=False,
        )

    def forward(self, features: ms.Tensor) -> ms.Tensor:
        residual = features

        features = self.expand_1x1(features)
        features = self.conv_3x3(features)
        features = self.reduce_1x1(features)

        return residual + features if self.use_residual else features


class MobileNetV2Stem(nn.Module):
    def __init__(self, config: MobileNetV2Config, in_channels: int, expanded_channels: int, out_channels: int) -> None:
        super().__init__()

        # The very first layer is a regular 3x3 convolution with stride 2 that expands to 32 channels.
        # All other expansion layers use the expansion factor to compute the number of output channels.
        self.first_conv = MobileNetV2ConvLayer(
            config,
            in_channels=in_channels,
            out_channels=expanded_channels,
            kernel_size=3,
            stride=2,
        )

        if config.first_layer_is_expansion:
            self.expand_1x1 = None
        else:
            self.expand_1x1 = MobileNetV2ConvLayer(
                config, in_channels=expanded_channels, out_channels=expanded_channels, kernel_size=1
            )

        self.conv_3x3 = MobileNetV2ConvLayer(
            config,
            in_channels=expanded_channels,
            out_channels=expanded_channels,
            kernel_size=3,
            stride=1,
            groups=expanded_channels,
        )

        self.reduce_1x1 = MobileNetV2ConvLayer(
            config,
            in_channels=expanded_channels,
            out_channels=out_channels,
            kernel_size=1,
            use_activation=False,
        )

    def forward(self, features: ms.Tensor) -> ms.Tensor:
        features = self.first_conv(features)
        if self.expand_1x1 is not None:
            features = self.expand_1x1(features)
        features = self.conv_3x3(features)
        features = self.reduce_1x1(features)
        return features


class MobileNetV2PreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = MobileNetV2Config
    base_model_prefix = "mobilenet_v2"
    main_input_name = "pixel_values"
    supports_gradient_checkpointing = False
    _no_split_modules = []
    _keys_to_ignore_on_load_unexpected = [r'num_batches_tracked']

    def _init_weights(self, cell: Union[nn.Linear, nn.Conv2d]) -> None:
        """Initialize the weights"""
        if isinstance(cell, (nn.Linear, nn.Conv2d)):
            cell.weight.assign_value(initializer(Normal(mean=0.0,sigma = self.config.initializer_range),
                                                cell.weight.shape,cell.weight.dtype))
            if cell.bias is not None:
                cell.bias.assign_value(initializer('zeros',cell.bias.shape, cell.bias.dtype))
        elif isinstance(cell, nn.BatchNorm2d):
            cell.weight.assign_value(initializer('ones',cell.weight.shape, cell.weight.dtype))
            cell.bias.assign_value(initializer('zeros',cell.bias.shape, cell.bias.dtype))


class MobileNetV2Model(MobileNetV2PreTrainedModel):
    def __init__(self, config: MobileNetV2Config, add_pooling_layer: bool = True):
        super().__init__(config)
        self.config = config

        # Output channels for the projection layers
        channels = [16, 24, 24, 32, 32, 32, 64, 64, 64, 64, 96, 96, 96, 160, 160, 160, 320]
        channels = [apply_depth_multiplier(config, x) for x in channels]

        # Strides for the depthwise layers
        strides = [2, 1, 2, 1, 1, 2, 1, 1, 1, 1, 1, 1, 2, 1, 1, 1]

        self.conv_stem = MobileNetV2Stem(
            config,
            in_channels=config.num_channels,
            expanded_channels=apply_depth_multiplier(config, 32),
            out_channels=channels[0],
        )

        current_stride = 2  # first conv layer has stride 2
        dilation = 1

        self.layer = nn.ModuleList()
        for i in range(16):
            # Keep making the feature maps smaller or use dilated convolution?
            if current_stride == config.output_stride:
                layer_stride = 1
                layer_dilation = dilation
                dilation *= strides[i]  # larger dilation starts in next block
            else:
                layer_stride = strides[i]
                layer_dilation = 1
                current_stride *= layer_stride

            self.layer.append(
                MobileNetV2InvertedResidual(
                    config,
                    in_channels=channels[i],
                    out_channels=channels[i + 1],
                    stride=layer_stride,
                    dilation=layer_dilation,
                )
            )

        if config.finegrained_output and config.depth_multiplier < 1.0:
            output_channels = 1280
        else:
            output_channels = apply_depth_multiplier(config, 1280)

        self.conv_1x1 = MobileNetV2ConvLayer(
            config,
            in_channels=channels[-1],
            out_channels=output_channels,
            kernel_size=1,
        )

        self.pooler = nn.AdaptiveAvgPool2d((1, 1)) if add_pooling_layer else None

        # Initialize weights and apply final processing
        self.post_init()

    def _prune_heads(self, heads_to_prune):
        raise NotImplementedError

    def forward(
        self,
        pixel_values: Optional[ms.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[tuple, BaseModelOutputWithPoolingAndNoAttention]:
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if pixel_values is None:
            raise ValueError("You have to specify pixel_values")

        hidden_states = self.conv_stem(pixel_values)

        all_hidden_states = () if output_hidden_states else None

        for i, layer_module in enumerate(self.layer):
            hidden_states = layer_module(hidden_states)

            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

        last_hidden_state = self.conv_1x1(hidden_states)

        if self.pooler is not None:
            pooled_output = ops.flatten(self.pooler(last_hidden_state), start_dim=1)
        else:
            pooled_output = None

        if not return_dict:
            return tuple(v for v in [last_hidden_state, pooled_output, all_hidden_states] if v is not None)

        return BaseModelOutputWithPoolingAndNoAttention(
            last_hidden_state=last_hidden_state,
            pooler_output=pooled_output,
            hidden_states=all_hidden_states,
        )



class MobileNetV2ForImageClassification(MobileNetV2PreTrainedModel):
    def __init__(self, config: MobileNetV2Config) -> None:
        super().__init__(config)

        self.num_labels = config.num_labels
        self.mobilenet_v2 = MobileNetV2Model(config)

        last_hidden_size = self.mobilenet_v2.conv_1x1.convolution.out_channels

        # Classifier head
        self.dropout = nn.Dropout(p = config.classifier_dropout_prob)
        self.classifier = nn.Linear(last_hidden_size, config.num_labels) if config.num_labels > 0 else nn.Identity()

        # Initialize weights and apply final processing
        self.post_init()


    def forward(
        self,
        pixel_values: Optional[ms.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        labels: Optional[ms.Tensor] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[tuple, ImageClassifierOutputWithNoAttention]:

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.mobilenet_v2(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

        pooled_output = outputs.pooler_output if return_dict else outputs[1]

        logits = self.classifier(self.dropout(pooled_output))

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and labels.dtype in (ms.int64, ms.int32):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                if self.num_labels == 1:
                    loss = F.mse_loss(logits.squeeze(), labels.squeeze())
                else:
                    loss = F.mse_loss(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss = F.cross_entropy(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss = F.binary_cross_entropy_with_logits(logits, labels)

        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return ImageClassifierOutputWithNoAttention(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
        )


class MobileNetV2DeepLabV3Plus(nn.Module):
    """
    The neural network from the paper "Encoder-Decoder with Atrous Separable Convolution for Semantic Image
    Segmentation" https://arxiv.org/abs/1802.02611
    """

    def __init__(self, config: MobileNetV2Config) -> None:
        super().__init__()

        self.avg_pool = nn.AdaptiveAvgPool2d(output_size=1)

        self.conv_pool = MobileNetV2ConvLayer(
            config,
            in_channels=apply_depth_multiplier(config, 320),
            out_channels=256,
            kernel_size=1,
            stride=1,
            use_normalization=True,
            use_activation="relu",
            layer_norm_eps=1e-5,
        )

        self.conv_aspp = MobileNetV2ConvLayer(
            config,
            in_channels=apply_depth_multiplier(config, 320),
            out_channels=256,
            kernel_size=1,
            stride=1,
            use_normalization=True,
            use_activation="relu",
            layer_norm_eps=1e-5,
        )

        self.conv_projection = MobileNetV2ConvLayer(
            config,
            in_channels=512,
            out_channels=256,
            kernel_size=1,
            stride=1,
            use_normalization=True,
            use_activation="relu",
            layer_norm_eps=1e-5,
        )

        self.dropout = nn.Dropout2d(config.classifier_dropout_prob)

        self.classifier = MobileNetV2ConvLayer(
            config,
            in_channels=256,
            out_channels=config.num_labels,
            kernel_size=1,
            use_normalization=False,
            use_activation=False,
            bias=True,
        )

    def forward(self, features: ms.Tensor) -> ms.Tensor:
        spatial_size = features.shape[-2:]

        features_pool = self.avg_pool(features)
        features_pool = self.conv_pool(features_pool)
        features_pool = F.interpolate(
            features_pool, size=spatial_size, mode="bilinear", align_corners=True
        )

        features_aspp = self.conv_aspp(features)

        features = ops.cat([features_pool, features_aspp], dim=1)

        features = self.conv_projection(features)
        features = self.dropout(features)
        features = self.classifier(features)
        return features



class MobileNetV2ForSemanticSegmentation(MobileNetV2PreTrainedModel):
    def __init__(self, config: MobileNetV2Config) -> None:
        super().__init__(config)

        self.num_labels = config.num_labels
        self.mobilenet_v2 = MobileNetV2Model(config, add_pooling_layer=False)
        self.segmentation_head = MobileNetV2DeepLabV3Plus(config)

        # Initialize weights and apply final processing
        self.post_init()


    def forward(
        self,
        pixel_values: Optional[ms.Tensor] = None,
        labels: Optional[ms.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[tuple, SemanticSegmenterOutput]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size, height, width)`, *optional*):
            Ground truth semantic segmentation maps for computing the loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels > 1`, a classification loss is computed (Cross-Entropy).

        Returns:

        Examples:

        ```python
        >>> from transformers import AutoImageProcessor, MobileNetV2ForSemanticSegmentation
        >>> from PIL import Image
        >>> import requests

        >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
        >>> image = Image.open(requests.get(url, stream=True).raw)

        >>> image_processor = AutoImageProcessor.from_pretrained("google/deeplabv3_mobilenet_v2_1.0_513")
        >>> model = MobileNetV2ForSemanticSegmentation.from_pretrained("google/deeplabv3_mobilenet_v2_1.0_513")

        >>> inputs = image_processor(images=image, return_tensors="ms")

        >>> with torch.no_grad():
        ...     outputs = model(**inputs)

        >>> # logits are of shape (batch_size, num_labels, height, width)
        >>> logits = outputs.logits
        ```"""
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if labels is not None and self.config.num_labels == 1:
            raise ValueError("The number of labels should be greater than one")

        outputs = self.mobilenet_v2(
            pixel_values,
            output_hidden_states=True,  # we need the intermediate hidden states
            return_dict=return_dict,
        )

        encoder_hidden_states = outputs.hidden_states if return_dict else outputs[1]

        logits = self.segmentation_head(encoder_hidden_states[-1])

        loss = None
        if labels is not None:
            # upsample logits to the images' original size
            upsampled_logits = F.interpolate(
                logits, size=labels.shape[-2:], mode="bilinear", align_corners=False
            )
            loss = F.cross_entropy(upsampled_logits, labels, ignore_index=self.config.semantic_loss_ignore_index)

        if not return_dict:
            if output_hidden_states:
                output = (logits,) + outputs[1:]
            else:
                output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return SemanticSegmenterOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states if output_hidden_states else None,
            attentions=None,
        )

__all__ = ['MobileNetV2ConvLayer','MobileNetV2InvertedResidual','MobileNetV2Stem',
           'MobileNetV2PreTrainedModel','MobileNetV2Model','MobileNetV2ForImageClassification',
           'MobileNetV2DeepLabV3Plus','MobileNetV2ForSemanticSegmentation']
