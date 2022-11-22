import torch
import numpy as np
import operator
from functools import reduce


DEFAULT_ACOS_BOUND: float = 1.0 - 1e-4


prod = lambda l: reduce(operator.mul, l, 1)


@torch.jit.script
def multiply_transform(w_rot_l, w_trans_l, l_rot_c, l_trans_c):

    l_trans_c = l_trans_c.float()
    w_rot_c = w_rot_l @ l_rot_c
    w_trans_c = (w_rot_l @ l_trans_c.unsqueeze(-1)).squeeze(-1) + w_trans_l

    return w_rot_c, w_trans_c


@torch.jit.script
def multiply_inv_transform(l_rot_w, l_trans_w, l_rot_c, l_trans_c):
    w_rot_l = l_rot_w.transpose(-1, -2)
    w_rot_c = w_rot_l @ l_rot_c

    w_trans_l = -(w_rot_l @ l_trans_w.unsqueeze(2)).squeeze(2)
    w_trans_c = (w_rot_l @ l_trans_c.unsqueeze(-1)).squeeze(-1) + w_trans_l

    return w_rot_c, w_trans_c


@torch.jit.script
def transform_point(point, rot, trans):
    new_point = (point @ rot.transpose(-1, -2)) + trans
    return new_point


def vector3_to_skew_symm_matrix(vec3):
    vec3 = to_torch_2d_min(vec3)
    batch_size = vec3.shape[0]
    skew_symm_mat = vec3.new_zeros((batch_size, 3, 3))
    skew_symm_mat[:, 0, 1] = -vec3[:, 2]
    skew_symm_mat[:, 0, 2] = vec3[:, 1]
    skew_symm_mat[:, 1, 0] = vec3[:, 2]
    skew_symm_mat[:, 1, 2] = -vec3[:, 0]
    skew_symm_mat[:, 2, 0] = -vec3[:, 1]
    skew_symm_mat[:, 2, 1] = vec3[:, 0]
    return skew_symm_mat


def quaternion_to_matrix(quaternions):
    """
    Convert rotations given as quaternions to rotation matrices.

    Args:
        quaternions: quaternions with real part first,
            as tensor of shape (..., 4).

    Returns:
        Rotation matrices as tensor of shape (..., 3, 3).
    """
    r, i, j, k = torch.unbind(quaternions, -1)
    two_s = 2.0 / (quaternions * quaternions).sum(-1)

    o = torch.stack(
        (
            1 - two_s * (j * j + k * k),
            two_s * (i * j - k * r),
            two_s * (i * k + j * r),
            two_s * (i * j + k * r),
            1 - two_s * (i * i + k * k),
            two_s * (j * k - i * r),
            two_s * (i * k - j * r),
            two_s * (j * k + i * r),
            1 - two_s * (i * i + j * j),
        ),
        -1,
    )
    return o.reshape(quaternions.shape[:-1] + (3, 3))



class MinMaxScaler():
    # NOTE: scale x, y to range [0, 1]
    def __init__(self, min=None, max=None, dim=None) -> None:
        self.min = min
        self.max = max
        self.dim = dim
    
    def scale(self, X):
        if isinstance(X, np.ndarray):
            if self.min is None:
                self.min = X.min(self.dim)  # link dim
            if self.max is None:
                self.max = X.max(self.dim)  # link dim
        else:
            if self.min is None:
                if self.dim is None:
                    self.min = X.detach().min()
                else:
                    self.min = X.detach().min(self.dim)[0]  # link dim
            if self.max is None:
                if self.max is None:
                    self.max = X.detach().max()
                else:
                    self.max = X.detach().min(self.dim)[0]  # link dim

        X_scaled = (X - self.min) / (self.max - self.min)
        return X_scaled


def euclidean_distance(x_batch, x_target, vel_batch=None, vel_target=None, w_pos=1., w_rot=0., w_vpos=1., w_vrot=0., broadcast_target=False, normalized_input=False):
    # NOTE: normalizing x inputs
    if x_batch.ndim == 1:
        x_batch = x_batch.unsqueeze(0)

    if x_target.ndim == 1:
        x_target = x_target.unsqueeze(0)

    dim_scale_target = -2
    if broadcast_target:
        x_target = x_target.unsqueeze(1)
        dim_scale_target = -3
    if normalized_input:
        scaler1, scaler2 = MinMaxScaler(dim=-2), MinMaxScaler(dim=dim_scale_target)
        x_batch = scaler1.scale(x_batch)
        x_target = scaler2.scale(x_target)
    T_distance = torch.linalg.norm(x_batch - x_target, dim=-1)
    D =  w_pos * T_distance

    # now compute vel distance
    if vel_batch is not None and vel_target is not None:
        if vel_target.ndim == 1:
            vel_target = vel_target.unsqueeze(0)
        if broadcast_target:
            vel_target = vel_target.unsqueeze(1)
        if normalized_input:  # normalize to range [0, 1]
            scaler1, scaler2 = MinMaxScaler(dim=-2), MinMaxScaler(dim=dim_scale_target)
            vel_batch = scaler1.scale(vel_batch)
            vel_target = scaler2.scale(vel_target)
        D += w_vpos * torch.linalg.norm(vel_batch - vel_target, dim=-1)

    return w_pos * T_distance


def SE3_distance(H_batch, H_target, vel_batch=None, vel_target=None, w_pos=1., w_rot=1., w_vpos=1., w_vrot=1., broadcast_target=False, normalized_input=False):
    # NOTE: normalizing x inputs
    if H_batch.ndim == 2:
        H_batch = H_batch.unsqueeze(0)

    if H_target.ndim == 2:
        H_target = H_target.unsqueeze(0)

    dim_scale_target = -2
    if broadcast_target:
        H_target = H_target.unsqueeze(1)
        dim_scale_target = -3

    x_batch, x_target = H_batch[..., :-1, -1], H_target[..., :-1, -1]
    if normalized_input:  # normalize to range [0, 1]
        scaler1, scaler2 = MinMaxScaler(dim=-2), MinMaxScaler(dim=dim_scale_target)
        x_batch = scaler1.scale(x_batch)
        x_target = scaler2.scale(x_target)
    D = 0.
    if w_rot > 0.:
        R_distance = (1 - so3_relative_angle(H_batch[..., :3, :3], H_target[..., :3, :3], cos_angle=True))
        D += w_rot * R_distance
    if w_pos > 0.:
        T_distance = torch.linalg.norm(x_batch - x_target, dim=-1)
        D += w_pos * T_distance

    # now compute vel distance
    if vel_batch is not None and vel_target is not None:
        v_trans, v_rot = vel_batch[0], vel_batch[1]
        v_trans_target, v_rot_target = vel_target[0], vel_target[1]
        if v_trans_target.ndim == 1:
            v_trans_target = v_trans_target.unsqueeze(0)
        if v_rot_target.ndim == 1:
            v_rot_target = v_rot_target.unsqueeze(0)
        if broadcast_target:
            v_trans_target = v_trans_target.unsqueeze(1)
            v_rot_target = v_rot_target.unsqueeze(1)
        if normalized_input:  # normalize to range [0, 1]
            v_trans_scaler1, v_rot_scaler1 = MinMaxScaler(dim=-2), MinMaxScaler(dim=-2)
            v_trans = v_trans_scaler1.scale(v_trans)
            v_rot = v_rot_scaler1.scale(v_rot)
            v_trans_scaler2, v_rot_scaler2 = MinMaxScaler(dim=dim_scale_target), MinMaxScaler(dim=dim_scale_target)
            v_trans_target = v_trans_scaler2.scale(v_trans_target)
            v_rot_target = v_rot_scaler2.scale(v_rot_target)
        if w_vpos > 0.:
            D += w_vpos * torch.linalg.norm(v_trans - v_trans_target, dim=-1)
        if w_vrot > 0.:
            D += w_vrot * torch.linalg.norm(v_rot - v_rot_target, dim=-1)
    return D


def R3S3_distance(x_batch, x_target, w_pos=1., w_rot=1.):
    if x_target.ndim == 1:
        x_target = x_target.unsqueeze(0)

    R_distance = quaternion_relative_angle(x_batch[..., 3:], x_target[..., 3:])
    T_distance = torch.linalg.norm(x_batch[..., :3] - x_target[..., :3], dim=-1)
    return w_pos * R_distance + w_rot * T_distance


def quaternion_relative_angle(q1, q2, cos_bound=1e-4):
    theta_cos = (q1 * q2).sum(-1)

    if cos_bound > 0.0:
        bound = 1.0 - cos_bound
        return acos_linear_extrapolation(theta_cos, (-bound, bound))
    else:
        return 2 * torch.acos(theta_cos)


def q_convert_xyzw(q):
    w, x, y, z = torch.unbind(q, dim=-1)
    return torch.stack([x, y, z, w], dim=-1)


def q_convert_wxyz(q):
    x, y, z, w = torch.unbind(q, dim=-1)
    return torch.stack([w, x, y, z], dim=-1)


def so3_relative_angle(R1, R2, eps=1e-4, cos_angle=False, cos_bound=1e-4):
    R12 = torch.matmul(R1, R2.transpose(-2, -1))
    return so3_rotation_angle(R12, eps=eps, cos_angle=cos_angle, cos_bound=cos_bound)


def so3_rotation_angle(R, eps=1e-4, cos_angle=False, cos_bound=1e-4):
    dim1, dim2 = R.shape[-2:]
    if dim1 != 3 or dim2 != 3:
        raise ValueError("Input has to be a batch of 3x3 Tensors.")
    rot_trace = R[..., 0, 0] + R[..., 1, 1] + R[..., 2, 2]

    # phi rotation angle
    phi_cos = (rot_trace - 1.0) * 0.5

    if cos_angle:
        return phi_cos
    else:
        if cos_bound > 0.0:
            bound = 1.0 - cos_bound
            return acos_linear_extrapolation(phi_cos, (-bound, bound))
        else:
            return torch.acos(phi_cos)


def acos_linear_extrapolation(x, bounds=(-(DEFAULT_ACOS_BOUND), DEFAULT_ACOS_BOUND)):
    lower_bound, upper_bound = bounds
    if lower_bound > upper_bound:
        raise ValueError("lower bound has to be smaller or equal to upper bound.")
    if lower_bound <= -1.0 or upper_bound >= 1.0:
        raise ValueError("Both lower bound and upper bound have to be within (-1, 1).")

    # init an empty tensor and define the domain sets
    acos_extrap = torch.empty_like(x)
    x_upper = x >= upper_bound
    x_lower = x <= lower_bound
    x_mid = (~x_upper) & (~x_lower)

    # acos calculation for upper_bound < x < lower_bound
    acos_extrap[x_mid] = torch.acos(x[x_mid])
    # the linear extrapolation for x >= upper_bound
    acos_extrap[x_upper] = _acos_linear_approximation(x[x_upper], upper_bound)
    # the linear extrapolation for x <= lower_bound
    acos_extrap[x_lower] = _acos_linear_approximation(x[x_lower], lower_bound)

    return acos_extrap


def _acos_linear_approximation(x: torch.Tensor, x0: float) -> torch.Tensor:
    """
    Calculates the 1st order Taylor expansion of `arccos(x)` around `x0`.
    """
    return (x - x0) * _dacos_dx(x0) + np.arccos(x0)


def _dacos_dx(x: float) -> float:
    """
    Calculates the derivative of `arccos(x)` w.r.t. `x`.
    """
    return (-1.0) / np.sqrt(1.0 - x * x)


def cross_product(vec3a, vec3b):
    vec3a = to_torch_2d_min(vec3a)
    vec3b = to_torch_2d_min(vec3b)
    skew_symm_mat_a = vector3_to_skew_symm_matrix(vec3a)
    return (skew_symm_mat_a @ vec3b.unsqueeze(2)).squeeze(2)


def bfill_lowertriangle(A: torch.Tensor, vec: torch.Tensor):
    ii, jj = np.tril_indices(A.size(-2), k=-1, m=A.size(-1))
    A[..., ii, jj] = vec
    return A


def bfill_diagonal(A: torch.Tensor, vec: torch.Tensor):
    ii, jj = np.diag_indices(min(A.size(-2), A.size(-1)))
    A[..., ii, jj] = vec
    return A

def torch_square(x):
    return x * x


def exp_map_so3(omega, epsilon=1.0e-14):
    omegahat = vector3_to_skew_symm_matrix(omega).squeeze()

    norm_omega = torch.norm(omega, p=2)
    exp_omegahat = (
        torch.eye(3)
        + ((torch.sin(norm_omega) / (norm_omega + epsilon)) * omegahat)
        + (
            ((1.0 - torch.cos(norm_omega)) / (torch_square(norm_omega + epsilon)))
            * (omegahat @ omegahat)
        )
    )
    return exp_omegahat


def to_torch_2d_min(variable):
    tensor_var = to_torch(variable)
    if len(tensor_var.shape) == 1:
        return tensor_var.unsqueeze(0)
    else:
        return tensor_var


def to_torch(variable):
    if isinstance(variable, torch.Tensor):
        return variable
    else:
        return torch.tensor(variable)