//   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
#include "paddle/fluid/framework/details/fused_all_reduce_op_handle.h"

#include "paddle/fluid/framework/convert_utils.h"
#include "paddle/fluid/framework/details/container_cast.h"
#include "paddle/fluid/framework/details/variable_visitor.h"
#include "paddle/fluid/platform/place.h"
#include "paddle/fluid/platform/profiler/event_tracing.h"
#include "paddle/phi/backends/device_memory_aligment.h"
#include "paddle/phi/core/flags.h"

PD_DEFINE_bool(skip_fused_all_reduce_check, false, "");  // NOLINT
PHI_DECLARE_bool(allreduce_record_one_event);

namespace paddle {
namespace framework {
namespace details {

typedef std::vector<
    std::vector<std::pair<std::string, const phi::DenseTensor *>>>
    GradientAndLoDTensor;

#if defined(PADDLE_WITH_NCCL) || defined(PADDLE_WITH_RCCL)
FusedAllReduceOpHandle::FusedAllReduceOpHandle(
    ir::Node *node,
    const std::vector<Scope *> &local_scopes,
    const std::vector<platform::Place> &places,
    const size_t num_of_all_reduce,
    const platform::NCCLCommunicator *ctxs)
    : AllReduceOpHandle(node, local_scopes, places, ctxs),
      num_of_all_reduce_(num_of_all_reduce) {}
#elif defined(PADDLE_WITH_XPU_BKCL)
FusedAllReduceOpHandle::FusedAllReduceOpHandle(
    ir::Node *node,
    const std::vector<Scope *> &local_scopes,
    const std::vector<platform::Place> &places,
    const size_t num_of_all_reduce,
    const platform::BKCLCommunicator *ctxs)
    : AllReduceOpHandle(node, local_scopes, places, ctxs),
      num_of_all_reduce_(num_of_all_reduce) {}
#else
FusedAllReduceOpHandle::FusedAllReduceOpHandle(
    ir::Node *node,
    const std::vector<Scope *> &local_scopes,
    const std::vector<platform::Place> &places,
    const size_t num_of_all_reduce)
    : AllReduceOpHandle(node, local_scopes, places),
      num_of_all_reduce_(num_of_all_reduce) {}
#endif

FusedAllReduceOpHandle::~FusedAllReduceOpHandle() {
#if defined(PADDLE_WITH_NCCL) || defined(PADDLE_WITH_RCCL)
  auto destroy_event = [](gpuEvent_t event) {
    if (event == nullptr) return;
#ifdef PADDLE_WITH_HIP
    PADDLE_ENFORCE_GPU_SUCCESS(hipEventDestroy(event));
#else
    PADDLE_ENFORCE_GPU_SUCCESS(cudaEventDestroy(event));
#endif
  };
  destroy_event(start_event_);
  destroy_event(end_event_);
#endif
}

void FusedAllReduceOpHandle::RunImpl() {
  platform::RecordEvent record_event(
      Name(), platform::TracerEventType::Communication, 1);
  VLOG(4) << this->DebugString();

#if defined(PADDLE_WITH_NCCL) || defined(PADDLE_WITH_RCCL)
  if (FLAGS_allreduce_record_one_event && start_event_ == nullptr) {
    VLOG(10) << "FLAGS_allreduce_record_one_event=true";
    PADDLE_ENFORCE_EQ(use_hierarchical_allreduce_,
                      false,
                      platform::errors::Unimplemented(
                          "The hierarchical allreduce does not support "
                          "FLAGS_allreduce_record_one_event=true"));
    PADDLE_ENFORCE_EQ(places_.size(),
                      1,
                      platform::errors::Unimplemented(
                          "FLAGS_allreduce_record_one_event=true is only valid "
                          "when using one GPU device per process."));
    PADDLE_ENFORCE_EQ(platform::is_gpu_place(places_[0]),
                      true,
                      platform::errors::Unimplemented(
                          "FLAGS_allreduce_record_one_event=true is only valid "
                          "when using GPU device."));
    auto create_event = [](gpuEvent_t *event) {
      if (*event) return;
#ifdef PADDLE_WITH_HIP
      PADDLE_ENFORCE_GPU_SUCCESS(
          hipEventCreateWithFlags(event, hipEventDisableTiming));
#else
      PADDLE_ENFORCE_GPU_SUCCESS(
          cudaEventCreateWithFlags(event, cudaEventDisableTiming));
#endif
    };
    create_event(&start_event_);
    create_event(&end_event_);
  }

  gpuStream_t nccl_stream{nullptr};
  gpuStream_t compute_stream{nullptr};

  if (FLAGS_allreduce_record_one_event) {
    auto gpu_place = platform::CUDAPlace(places_[0].GetDeviceId());
    compute_stream =
        platform::DeviceContextPool::Instance().GetByPlace(gpu_place)->stream();
    auto flat_nccl_ctxs = nccl_ctxs_->GetFlatCtx(run_order_);
    auto &nccl_ctx = flat_nccl_ctxs->at(gpu_place.device);
    nccl_stream = nccl_ctx.stream();
#ifdef PADDLE_WITH_HIP
    PADDLE_ENFORCE_GPU_SUCCESS(hipEventRecord(start_event_, compute_stream));
    PADDLE_ENFORCE_GPU_SUCCESS(
        hipStreamWaitEvent(nccl_stream, start_event_, 0));
#else
    PADDLE_ENFORCE_GPU_SUCCESS(cudaEventRecord(start_event_, compute_stream));
    PADDLE_ENFORCE_GPU_SUCCESS(
        cudaStreamWaitEvent(nccl_stream, start_event_, 0));
#endif
  } else {
    WaitInputVarGenerated();
  }
#else
  WaitInputVarGenerated();
#endif

  // The input: grad0(dev0), grad0(dev1), grad1(dev0), grad1(dev1)...
  // The output: grad0(dev0), grad0(dev1), grad1(dev0), grad1(dev1)...
  auto in_var_handles = DynamicCast<VarHandle>(this->Inputs());
  auto out_var_handles = DynamicCast<VarHandle>(this->Outputs());

  size_t place_num = places_.size();
  PADDLE_ENFORCE_EQ(
      in_var_handles.size(),
      place_num * num_of_all_reduce_,
      platform::errors::PreconditionNotMet(
          "The number of input variable handles should be equal to the number "
          "of places plus the number of all reduce handles, "
          "but got the number of input variable handles is %d, the "
          "number of places is %d, and the number of all reduce handles "
          "is %d.",
          in_var_handles.size(),
          place_num,
          num_of_all_reduce_));
  PADDLE_ENFORCE_EQ(
      in_var_handles.size(),
      out_var_handles.size(),
      platform::errors::PreconditionNotMet(
          "The number of input variable handles should be equal to the number "
          "of output variable handles, but got the number of input variable "
          "handles is %d, and the number of  output variable handles is %d.",
          in_var_handles.size(),
          out_var_handles.size()));

  // Note: some gradient op doesn't have CUDAKernel or XPUKernel, so the
  // gradients of those op are in CPUPlace, in this case, the all reduce
  // should not be fused.
  if (InputIsInDifferentPlace(in_var_handles)) {
    for (size_t j = 0; j < num_of_all_reduce_; ++j) {
      std::vector<VarHandle *> dev_inputs;
      std::vector<VarHandle *> dev_outputs;
      dev_inputs.reserve(place_num);
      dev_outputs.reserve(place_num);
      for (size_t idx = 0; idx < place_num; ++idx) {
        dev_inputs.emplace_back(in_var_handles.at(j * place_num + idx));
        dev_outputs.emplace_back(out_var_handles.at(j * place_num + idx));
      }
      AllReduceImpl(dev_inputs, dev_outputs);
    }
  } else {
    FusedAllReduceFunc(in_var_handles, out_var_handles);
  }

#if defined(PADDLE_WITH_NCCL) || defined(PADDLE_WITH_RCCL)
  if (FLAGS_allreduce_record_one_event) {
#ifdef PADDLE_WITH_HIP
    PADDLE_ENFORCE_GPU_SUCCESS(hipEventRecord(end_event_, nccl_stream));
    PADDLE_ENFORCE_GPU_SUCCESS(
        hipStreamWaitEvent(compute_stream, end_event_, 0));
#else
    PADDLE_ENFORCE_GPU_SUCCESS(cudaEventRecord(end_event_, nccl_stream));
    PADDLE_ENFORCE_GPU_SUCCESS(
        cudaStreamWaitEvent(compute_stream, end_event_, 0));
#endif
  }
#endif
}

void FusedAllReduceOpHandle::FusedAllReduceFunc(
    const std::vector<VarHandle *> &in_var_handles,
    const std::vector<VarHandle *> &out_var_handles) {
  size_t place_num = places_.size();

  GradientAndLoDTensor grads_tensor;
  grads_tensor.resize(place_num);

  int64_t numel = -1;
  auto dtype = static_cast<framework::proto::VarType::Type>(0);
  for (size_t scope_idx = 0; scope_idx < local_scopes_.size(); ++scope_idx) {
    auto &g_tensor = grads_tensor.at(scope_idx);
    g_tensor.reserve(num_of_all_reduce_);

    GetGradLoDTensor(scope_idx, in_var_handles, out_var_handles, &g_tensor);

    int64_t element_num = 0;
    framework::proto::VarType::Type ele_dtype =
        static_cast<framework::proto::VarType::Type>(0);
    GetDTypeAndNumel(g_tensor, &ele_dtype, &element_num);

    if (scope_idx == 0) {
      numel = element_num;
      dtype = ele_dtype;
    }

    PADDLE_ENFORCE_EQ(
        ele_dtype,
        dtype,
        platform::errors::InvalidArgument(
            "The DataType of grad tensors of fused_all_reduce_op_handle  "
            "must be consistent. The current dtype is %s, but the "
            "previous dtype is %s.",
            DataTypeToString(ele_dtype),
            DataTypeToString(dtype)));

    // Check whether the address space is contiguous.
    std::sort(
        g_tensor.begin(),
        g_tensor.end(),
        [](const std::pair<std::string, const phi::DenseTensor *> &grad1,
           const std::pair<std::string, const phi::DenseTensor *> &grad2)
            -> bool { return grad1.second->data() < grad2.second->data(); });

    size_t size_of_dtype = framework::SizeOfType(dtype);
    for (size_t k = 1; k < g_tensor.size(); ++k) {
      const void *cur_address = g_tensor.at(k - 1).second->data();
      int64_t len = g_tensor.at(k - 1).second->numel();
      auto offset = phi::Alignment(len * size_of_dtype, places_[0]);
      void *infer_next_address = reinterpret_cast<void *>(
          reinterpret_cast<uintptr_t>(cur_address) + offset);
      const void *next_address = g_tensor.at(k).second->data();

      VLOG(10) << string::Sprintf(
          "Input[%d](%s) address: 0X%02x, Input[%d](%s) address: 0X%02x, Infer "
          "input[%d] address: 0X%02x. The offset: %d",
          k - 1,
          g_tensor.at(k - 1).first,
          cur_address,
          g_tensor.at(k).first,
          k,
          next_address,
          k,
          infer_next_address,
          offset);
      PADDLE_ENFORCE_EQ(
          infer_next_address,
          next_address,
          platform::errors::InvalidArgument(
              "The infered address of the next tensor should be equal to the "
              "real address of the next tensor. But got infered address is %p "
              "and real address is %p.",
              infer_next_address,
              next_address));
    }
  }

  if (!FLAGS_skip_fused_all_reduce_check) {
    for (size_t scope_idx = 0; scope_idx < place_num; ++scope_idx) {
      for (size_t j = 1; j < num_of_all_reduce_; ++j) {
        PADDLE_ENFORCE_EQ(
            grads_tensor.at(0).at(j).first,
            grads_tensor.at(scope_idx).at(j).first,
            platform::errors::InvalidArgument(
                "The variable name of grad tensors of "
                "fused_all_reduce_op_handle  "
                "must be consistent. The current name is %s, but the "
                "previous name is %s.",
                grads_tensor.at(0).at(j).first,
                grads_tensor.at(scope_idx).at(j).first));
      }
    }
  }

  std::vector<const void *> lod_tensor_data;
  lod_tensor_data.reserve(place_num);
  for (size_t scope_idx = 0; scope_idx < place_num; ++scope_idx) {
    auto data = grads_tensor.at(scope_idx).at(0).second->data();
    lod_tensor_data.emplace_back(data);
  }
  std::vector<std::string> grad_var_names;
  grad_var_names.reserve(place_num);
  for (auto &grad_t : grads_tensor) {
    grad_var_names.emplace_back(grad_t.at(0).first);
  }

  AllReduceFunc(lod_tensor_data, dtype, numel, this->places_, grad_var_names);
}

bool FusedAllReduceOpHandle::InputIsInDifferentPlace(
    const std::vector<VarHandle *> &in_var_handles) const {
  for (size_t scope_idx = 0; scope_idx < local_scopes_.size(); ++scope_idx) {
    auto *local_scope = local_exec_scopes_[scope_idx];
    size_t place_num = places_.size();
    for (size_t j = 0; j < in_var_handles.size(); j += place_num) {
      auto var_name = in_var_handles[j]->name();
      auto var = local_scope->FindVar(var_name);
      PADDLE_ENFORCE_NOT_NULL(
          var,
          platform::errors::NotFound(
              "The variable '%s' is not found in local scope.", var_name));
      auto &lod_tensor = var->Get<phi::DenseTensor>();
      if (!platform::is_same_place(lod_tensor.place(), places_.at(scope_idx))) {
        return true;
      }
    }
  }
  return false;
}

void FusedAllReduceOpHandle::GetGradLoDTensor(
    const size_t &scope_idx,
    const std::vector<VarHandle *> &in_var_handles,
    const std::vector<VarHandle *> &out_var_handles,
    std::vector<std::pair<std::string, const phi::DenseTensor *>> *grad_tensor)
    const {
  auto *local_scope = local_exec_scopes_[scope_idx];
  size_t place_num = places_.size();
  for (size_t j = 0; j < in_var_handles.size(); j += place_num) {
    auto var_name = in_var_handles[j]->name();
    PADDLE_ENFORCE_EQ(
        var_name,
        out_var_handles[j]->name(),
        platform::errors::InvalidArgument(
            "The name of input variable should be equal "
            "to the name of output variable. But got the name of input "
            "variable is %s and the name of output variable is %s.",
            var_name,
            out_var_handles[j]->name()));
    auto var = local_scope->FindVar(var_name);
    PADDLE_ENFORCE_NOT_NULL(
        var,
        platform::errors::NotFound(
            "The variable '%s' is not found in local scope.", var_name));
    auto &lod_tensor = var->Get<phi::DenseTensor>();

    PADDLE_ENFORCE_EQ(
        platform::is_same_place(lod_tensor.place(), places_.at(scope_idx)),
        true,
        platform::errors::InvalidArgument(
            "The variable '%s' at scope %d is not in the right place.",
            var_name,
            scope_idx));
    grad_tensor->emplace_back(std::make_pair(var_name, &lod_tensor));
  }
}

void FusedAllReduceOpHandle::GetDTypeAndNumel(
    const std::vector<std::pair<std::string, const phi::DenseTensor *>>
        &grad_tensor,
    proto::VarType::Type *dtype,
    int64_t *numel) const {
  *numel = 0;
  size_t size_of_dtype = 0;
  for (size_t i = 0; i < grad_tensor.size(); ++i) {
    // Get dtype
    auto ele_dtype =
        framework::TransToProtoVarType(grad_tensor.at(i).second->dtype());
    if (i == 0) {
      *dtype = ele_dtype;
      size_of_dtype = framework::SizeOfType(ele_dtype);
    }
    PADDLE_ENFORCE_EQ(
        ele_dtype,
        *dtype,
        platform::errors::InvalidArgument(
            "The DataType of grad tensors of fused_all_reduce_op_handle  "
            "must be consistent. The current dtype is %s, but the "
            "previous dtype is %s.",
            DataTypeToString(ele_dtype),
            DataTypeToString(*dtype)));

    // Get element number
    int64_t len = grad_tensor.at(i).second->numel();
    PADDLE_ENFORCE_GT(
        len,
        0,
        platform::errors::InvalidArgument(
            "The size of grad tensors of fused_all_reduce_op_handle  "
            "must be > 0, but got %d.",
            len));
    *numel += static_cast<int64_t>(
        phi::Alignment(len * size_of_dtype, places_[0]) / size_of_dtype);
  }
}

std::string FusedAllReduceOpHandle::Name() const { return "fused_all_reduce"; }
}  // namespace details
}  // namespace framework
}  // namespace paddle
