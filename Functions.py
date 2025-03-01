import torch
from torch import autograd
from torch import nn
import torchvision
from torch import optim
import torchvision.transforms as transforms
from tqdm.notebook import tqdm
import numpy as np
import torch.nn.functional as F

class BackPool(autograd.Function):

    # Note that both forward and backward are @staticmethods
    @staticmethod
    def forward(ctx, x, xS, a):
        ctx.a = a
        return x, xS

    # This function has only a single output, so it gets only one gradient
    @staticmethod
    def backward(ctx, grad_output, grad_outputS):

        grad_x = grad_output
        grad_xS = grad_outputS * ctx.a

        return grad_x, grad_xS, None

class TimesFunction(autograd.Function):

    # Note that both forward and backward are @staticmethods
    @staticmethod
    def forward(ctx, x, xS, a):
        ctx.a = a
        return x * a, torch.ones_like(x)

    # This function has only a single output, so it gets only one gradient
    @staticmethod
    def backward(ctx, grad_output, grad_outputS):

        grad_x = grad_output * ctx.a
        grad_xS = grad_output * (ctx.a ** 2)

        return grad_x, grad_xS, None


class SLinearFunction(autograd.Function):

    # Note that both forward and backward are @staticmethods
    @staticmethod
    # bias is an optional argument
    def forward(ctx, input, inputS, weight, weightS, bias=None):
        ctx.save_for_backward(input, weight, bias)
        # output = input.mm(weight.t())
        # # outputS = inputS.mm(weightS.t())
        # if bias is not None:
        #     output += bias.unsqueeze(0).expand_as(output)
        output = F.linear(input, weight, bias)
        return output, torch.ones_like(output)#outputS

    # This function has only a single output, so it gets only one gradient
    @staticmethod
    def backward(ctx, grad_output, grad_outputS):
        # This is a pattern that is very convenient - at the top of backward
        # unpack saved_tensors and initialize all gradients w.r.t. inputs to
        # None. Thanks to the fact that additional trailing Nones are
        # ignored, the return statement is simple even when the function has
        # optional inputs.
        input, weight, bias = ctx.saved_tensors
        grad_input = grad_weight = grad_inputS = grad_bias = grad_weightS = None

        # print(f"g: {grad_output}")
        # These needs_input_grad checks are optional and there only to
        # improve efficiency. If you want to make your code simpler, you can
        # skip them. Returning gradients for inputs that don't require it is
        # not an error.
        if ctx.needs_input_grad[0]:
            grad_input = grad_output.mm(weight)
        if ctx.needs_input_grad[1]:
            grad_inputS = grad_outputS.mm(weight**2)
        if ctx.needs_input_grad[2]:
            grad_weight = grad_output.t().mm(input)
        if ctx.needs_input_grad[3]:
            grad_weightS = grad_outputS.t().mm(input**2)
        if bias is not None and ctx.needs_input_grad[4]:
            grad_bias = grad_output.sum(0)

        return grad_input, grad_inputS, grad_weight, grad_weightS, grad_bias

class SConv2dFunction(autograd.Function):
    @staticmethod
    def forward(ctx, input, inputS, weight, weightS, bias=None, stride=1, padding=0, dilation=1, groups=1):
        # col_weights = weight.reshape(weight.shape[0], -1).swapaxes(0,1)
        # input = F.pad(input,tuple(4*[padding]))
        # bs, xc, xw, xh = input.shape
        # oc, _, kw, kh = weight.shape
        # ow, oh = xw - kw + 1, xh - kh + 1

        # col_image = F.unfold(input,(kw,kh)).transpose(1,2)
        # conv_out = col_image.matmul(w.view(w.size(0),-1).t()).transpose(1,2)
        # conv_out = F.fold(conv_out, (ow, oh), (1,1))
        # ctx.save_for_backward(col_image, weight, bias)
        conv_out = F.conv2d(input, weight, bias, stride, padding, dilation, groups)
        padding = padding[0]
        padded_input = F.pad(input,tuple(4*[padding]))
        ctx.stride = stride
        ctx.groups = groups
        ctx.save_for_backward(padded_input, weight, bias, torch.IntTensor([padding]).to(padded_input.device))
        return conv_out, torch.ones_like(conv_out)
    
    @staticmethod
    def backward(ctx, grad_output_ori, grad_outputS_ori):
        input_ori, weight_ori, bias, padding = ctx.saved_tensors
        stride = ctx.stride
        o_size = grad_output_ori.shape
        new_o = torch.zeros(o_size[0], o_size[1], o_size[2] * stride[0], o_size[3] * stride[1]).to(grad_output_ori.device)
        new_o[:,:,::stride[0],::stride[1]] = grad_output_ori
        new_oS = torch.zeros(o_size[0], o_size[1], o_size[2] * stride[0], o_size[3] * stride[1]).to(grad_outputS_ori.device)
        new_oS[:,:,::stride[0],::stride[1]] = grad_outputS_ori
        grad_output_ori = new_o
        grad_outputS_ori = new_oS
        
        oc, ic, kw, kh = weight_ori.shape
        block_o = oc // ctx.groups
        block_i = ic
        grad_w = []
        grad_wS = []
        grad_b = []
        grad_i = []
        grad_iS = []
        for i in range(ctx.groups):
            weight = weight_ori[block_o*i:block_o*(i+1),:,:,:]
            input = input_ori[:,block_i*i:block_i*(i+1),:,:]
            grad_output = grad_output_ori[:,block_o*i:block_o*(i+1),:,:]
            grad_outputS = grad_outputS_ori[:,block_o*i:block_o*(i+1),:,:]

            oc, ic, kw, kh = weight.shape
            col_image = F.unfold(input,(kw,kh)).transpose(1,2)
            bs, channels, ow, oh = grad_output.shape
            
            # col_grad_output = grad_output.view(bs, channels, -1)
            grad_w.append(grad_output.view(bs, channels, -1).bmm(col_image).sum(dim=0).view(weight.shape))
            grad_wS.append(grad_outputS.view(bs, channels, -1).bmm(col_image**2).sum(dim=0).view(weight.shape)) # SSSS

            if bias is None:
                grad_b = None
            else:
                grad_b.append(grad_output.sum(axis=[0,2,3]))

            grad_output_padded = F.pad(grad_output,tuple(4*[kw-1-padding.item()]))
            col_grad = F.unfold(grad_output_padded,(kh,kw)).transpose(1,2)
            grad_outputS_padded = F.pad(grad_outputS,tuple(4*[kw-1-padding.item()])) # SSSS
            col_gradS = F.unfold(grad_outputS_padded,(kh,kw)).transpose(1,2)
            
            flipped_w = weight.flip([2,3]).swapaxes(0,1)
            col_flip = flipped_w.reshape(flipped_w.size(0),-1)
            grad_i_this = col_grad.matmul(col_flip.t()).transpose(1,2)
            grad_i.append(F.fold(grad_i_this, (ow, oh), (1,1)))
            grad_iS_this  = col_gradS.matmul(col_flip.t() ** 2).transpose(1,2)
            grad_iS.append(F.fold(grad_iS_this, (ow, oh), (1,1)))
        
        grad_w = torch.cat(grad_w, dim=0)
        grad_wS = torch.cat(grad_wS, dim=0)
        grad_i = torch.cat(grad_i, dim=1)
        grad_iS = torch.cat(grad_iS, dim=1)
        if bias is not None:
            grad_b = torch.cat(grad_b, dim=0)

        return grad_i, grad_iS, grad_w, grad_wS, grad_b, None, None, None, None


class SBatchNorm2dFunction(autograd.Function):
    @staticmethod
    # bias is an optional argument
    def forward(ctx, input, inputS, running_mean, running_var, weight=None, bias=None, training=False, momentum=0.1, eps=1e-05):
        function = torch.nn.functional.batch_norm
        output = function(input, running_mean, running_var, weight, bias, training, momentum, eps)
        ctx.save_for_backward(input, running_mean, running_var, weight, bias, torch.Tensor([eps]).to(weight.device))
        return output, torch.ones_like(output)

    @staticmethod
    def backward(ctx, grad_output, grad_outputS):
        
        input, running_mean, running_var, weight, bias, eps = ctx.saved_tensors
        running_mean = running_mean.view(1,-1,1,1)
        running_var = running_var.view(1,-1,1,1)
        weight = weight.view(1,-1,1,1)
        skr = torch.sqrt(running_var + eps)
        if weight is not None:
            grad_weight = ((input - running_mean) / skr).sum(dim=[0,2,3])
        else:
            weight = 1
            grad_weight = None
        if bias is not None:
            grad_bias = grad_output.sum(axis=[0,2,3])
        else:
            bias = 0
            grad_bias = None
        grad_input = grad_output * weight / skr
        grad_inputS = grad_outputS * ((weight / skr) ** 2)
        # grad_inputS = grad_outputS * ((weight **2 / skr))
        

        return grad_input, grad_inputS, None, None, grad_weight, grad_bias, None, None, None

class QuantFunction(autograd.Function):
    @staticmethod
    def forward(ctx, N, input):
        det = input.abs().max() / pow(2, N)
        if det == 0:
            return input
        else:
            return (input/det).round() * det

    @staticmethod
    def backward(ctx, grad_output):
        return None, grad_output

class SMSEFunction(autograd.Function):
    @staticmethod
    # bias is an optional argument
    def forward(ctx, input, inputS, target, size_average=None, reduce=None, reduction='mean'):
        function = torch.nn.functional.mse_loss
        output = function(input, target, size_average, reduce, reduction)
        ctx.save_for_backward(input, target)
        return output

    # This function has only a single output, so it gets only one gradient
    @staticmethod
    def backward(ctx, grad_output):
        # This is a pattern that is very convenient - at the top of backward
        # unpack saved_tensors and initialize all gradients w.r.t. inputs to
        # None. Thanks to the fact that additional trailing Nones are
        # ignored, the return statement is simple even when the function has
        # optional inputs.
        input, target = ctx.saved_tensors
        grad_input = 2 * (input - target)

        return grad_input, torch.ones_like(grad_input) * 2, None, None, None, None

def is_nan(x):
    return torch.isnan(x).sum() != 0

def nan_print(x):
    x = x.tolist()
    for i in x:
        print(i)

def test_nan(exp, exp_sum, g_input, g_inputS, ratio):
    if is_nan(g_input) or is_nan(g_inputS):
        torch.save([exp.cpu().numpy(), exp_sum.cpu().numpy()], "debug.pt")
        print(is_nan(g_input), is_nan(g_inputS))
        raise Exception

class SCrossEntropyLossFunction(autograd.Function):
    @staticmethod
    # bias is an optional argument
    def forward(ctx, input, inputS, target, weight=None, size_average=None, ignore_index=-100, reduce=None, reduction='mean'):
        function = torch.nn.functional.cross_entropy
        output = function(input, target, weight, size_average, ignore_index, reduce, reduction)
        ctx.save_for_backward(input, target)
        return output

    # This function has only a single output, so it gets only one gradient
    @staticmethod
    def backward(ctx, grad_output):
        # This is a pattern that is very convenient - at the top of backward
        # unpack saved_tensors and initialize all gradients w.r.t. inputs to
        # None. Thanks to the fact that additional trailing Nones are
        # ignored, the return statement is simple even when the function has
        # optional inputs.
        eps = pow(2,-10)
        input, target = ctx.saved_tensors

        the_max = torch.max(input, dim=1)[0].unsqueeze(1).expand_as(input)
        exp = torch.exp(input - the_max)
        exp_sum = exp.sum(dim=1).unsqueeze(1).expand_as(input)
        ratio = exp / exp_sum

        grad_input_mask = torch.zeros_like(input)
        l_index = torch.LongTensor(range(len(input))).to(grad_input_mask.device)
        grad_input_mask[l_index, target] = 1
        grad_input = (ratio - grad_input_mask)/len(input)
        # grad_inputS = (exp_sum - exp) * exp / (exp_sum ** 2)
        # grad_input = (ratio - grad_input_mask)/len(input)
        grad_inputS = (1 - ratio) * ratio
        
        test_nan(exp, exp_sum, grad_input, grad_inputS, ratio)

        return grad_input, grad_inputS, None, None, None, None, None, None
