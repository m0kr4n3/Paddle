# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
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

import unittest

import paddle
from paddle.jit.sot.utils import with_export_guard


class Net(paddle.nn.Layer):
    def __init__(self):
        super().__init__()
        self.linear = paddle.nn.Linear(2, 2)
        self.bias = self.create_parameter(
            shape=[2],
            attr=None,
            dtype="float32",
            is_bias=True,
        )

    def forward(self, x):
        a = self.linear(x)
        return a + self.bias


class TestSotExport(unittest.TestCase):
    @with_export_guard("/tmp")
    def test_basic(self):
        net = Net()
        x = paddle.to_tensor([2, 3], dtype="float32", stop_gradient=True)
        y = paddle.jit.to_static(net)(x)


if __name__ == "__main__":
    unittest.main()
