# Copyright 2023 Huawei Technologies Co., Ltd
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
# ============================================================================
"""
MaskFormer Models init
"""
from . import configuration_maskformer, modeling_maskformer, feature_extraction_maskformer, image_processing_maskformer, \
    configuration_maskformer_swin, modeling_maskformer_swin
from .configuration_maskformer import *
from .configuration_maskformer_swin import *
from .modeling_maskformer import *
from .feature_extraction_maskformer import *
from .image_processing_maskformer import *
from .modeling_maskformer_swin import *

__all__ = []
__all__.extend(modeling_maskformer.__all__)
__all__.extend(modeling_maskformer_swin.__all__)
__all__.extend(configuration_maskformer.__all__)
__all__.extend(configuration_maskformer_swin.__all__)
__all__.extend(feature_extraction_maskformer.__all__)
__all__.extend(image_processing_maskformer.__all__)
