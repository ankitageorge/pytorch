# mypy: allow-untyped-defs
from typing import Callable, Tuple, Union

import torch
import torch.utils._pytree as pytree
from torch._C import DispatchKey
from torch._higher_order_ops.utils import (
    _has_potential_branch_input_alias,
    _has_potential_branch_input_mutation,
    _maybe_run_with_interpreter,
    _set_compilation_env,
    autograd_not_implemented,
    reenter_make_fx,
    UnsupportedAliasMutationException,
    validate_subgraph_args_types,
)
from torch._ops import HigherOrderOperator
from torch._subclasses.fake_tensor import FakeTensorMode
from torch.fx.experimental.proxy_tensor import (
    _temp_remove_metadata_torch_function_mode,
    ProxyTorchDispatchMode,
    track_tensor_tree,
)


class WhileLoopOp(HigherOrderOperator):
    def __init__(self) -> None:
        super().__init__("while_loop")

    def __call__(
        self,
        cond_fn: Callable,
        body_fn: Callable,
        carried_inputs: Tuple[Union[torch.Tensor, int, float, bool]],
        additional_inputs: Tuple[Union[torch.Tensor, int, float, bool]],
        /,
    ):
        if not isinstance(carried_inputs, tuple):
            raise RuntimeError(
                f"carried_inputs must be a tuple, got {type(carried_inputs)}"
            )
        if not isinstance(additional_inputs, tuple):
            raise RuntimeError(
                f"additional_inputs must be a tuple, got {type(additional_inputs)}"
            )

        if not all(
            isinstance(t, (torch.Tensor, int, float, bool, torch.SymInt))
            for t in carried_inputs
        ):
            raise RuntimeError(
                "carried_inputs must be a tuple of tensors, ints, floats, or bools, got "
                f"{carried_inputs}"
            )

        validate_subgraph_args_types(additional_inputs)
        return super().__call__(cond_fn, body_fn, carried_inputs, additional_inputs)


while_loop_op = WhileLoopOp()


def while_loop(cond_fn, body_fn, carried_inputs):
    r"""
    Run body_fn(*carried_inputs) while cond_fn(*carried_inputs) returns a True scalar tensor. Returns the output of body_fn or
    initial carried_inputs.

    .. warning::
        `torch.while_loop` is a prototype feature in PyTorch. It has limited support for input and output types and
        doesn't support training currently. Please look forward to a more stable implementation in a future version of PyTorch.
        Read more about feature classification at: https://pytorch.org/blog/pytorch-feature-classification-changes/#prototype

    `while_loop` is a structured control flow operator. It preserves the loop semantic across the torch.compile and torch.export.

    `while_loop` is equivalent to the following:

        def while_loop(cond_fn, body_fn, carried_inputs):
            val = carried_inputs
            while cond_fn(*val):
                val = body_fn(*val)
            return val

    Args:
        cond_fn (Callable): A callable function that returns a boolean Scalar tensor.

        body_fn (Callable): A callable function that takes the same inputs as `cond_fn` and returns a tuple of tensors

        carried_inputs (Tuple of possibly nested dict/list/tuple of tensors): A tuple of inputs to cond_fn and body_fn. It's also
            the initial value of states that are carried across iterations.

    Example:

        def cond_fn(iter, x):
            return iter.sum() < 10

        def body_fn(iter, x):
            return iter + 1, x.sin()

        while_loop(cond_fn, body_fn, (torch.zeros(1), torch.randn(3, 4)))

    Restrictions:

        - body_fn must return tensors with the same metadata (e.g.shape, dtype) as inputs.

        - body_fn and cond_fn must not in-place mutate the carried_inputs. A clone before the mutation is required.

        - body_fn and cond_fn must not mutate python varialbles (e.g. list/dict) created outside of the body_fn.

        - body_fn and cond_fn's output cannot aliase any of the inputs. A clone is required.

    .. warning::
        Temporal Limitations:

        - 'while_loop' only supports **inference** right now. Autograd will be supported in the future.

    """
    from torch._dynamo.backends.debugging import (
        make_eager_backend_with_torch_function_mode,
    )

    # Currently, additional_inputs is not a user-facing input. It will be automatically set in dynamo.
    # parameters and buffers accessed in cond_fn or body_fn or tensor closures will become additional_inputs.
    additional_inputs: Tuple = ()
    if torch.compiler.is_dynamo_compiling():
        return while_loop_op(cond_fn, body_fn, carried_inputs, additional_inputs)

    def _validate_input(cond_fn, body_fn, carried_inputs):
        from torch._higher_order_ops.utils import validate_subgraph_args_types

        if not callable(cond_fn) or not callable(body_fn):
            raise RuntimeError("Expect cond_fn and body_fn to be callable.")

        validate_subgraph_args_types(carried_inputs)

    _validate_input(cond_fn, body_fn, carried_inputs)

    # Dynamo is expecting a callable with "__code__" attribute.
    # We cannot directly pass cond_op to it. So we wrap it in a dummy function.
    def _while_loop_op_wrapper(*args, **kwargs):
        return while_loop_op(*args, **kwargs)

    with _set_compilation_env(), torch._dynamo.utils.disable_cache_limit():
        with _temp_remove_metadata_torch_function_mode() as metadata_mode:
            with _temp_remove_metadata_torch_function_mode() as metadata_mode:
                if metadata_mode:
                    backend = make_eager_backend_with_torch_function_mode(metadata_mode)
                else:
                    backend = "eager"
                return torch.compile(
                    _while_loop_op_wrapper, backend=backend, fullgraph=True
                )(cond_fn, body_fn, carried_inputs, additional_inputs)


@while_loop_op.py_impl(DispatchKey.CompositeExplicitAutograd)
def while_loop_dense(cond_fn, body_fn, carried_inputs, additional_inputs):
    carried_vals = carried_inputs

    def _is_valid_cond_fn_output(pred):
        return isinstance(pred, bool) or (
            isinstance(pred, torch.Tensor)
            and pred.size() == torch.Size([])
            and pred.dtype == torch.bool
        )

    if not isinstance(carried_inputs, tuple):
        raise RuntimeError(
            f"carried_inputs must be a tuple but got {type(carried_inputs)}"
        )

    while pred := cond_fn(*carried_vals, *additional_inputs):
        if not _is_valid_cond_fn_output(pred):
            raise RuntimeError(
                f"cond_fn must return a boolean scalar tensor or a bool but got {pred}"
            )
        out = body_fn(*carried_vals, *additional_inputs)
        assert isinstance(
            out, tuple
        ), f"body_fn should return a tuple but got {type(out)}"
        assert len(out) == len(
            carried_inputs
        ), "body_fn should return the same number of elements as carried_inputs"
        carried_vals = out
    return carried_vals


while_loop_op.py_impl(DispatchKey.Autograd)(
    autograd_not_implemented(while_loop_op, deferred_error=True)
)


def _unspecialize_int(fake_mode, t, compute_binding):
    from torch.fx.experimental.symbolic_shapes import compute_unbacked_bindings

    assert isinstance(t, int), t
    unbacked_idx = fake_mode.shape_env.create_unbacked_symint()
    if compute_binding:
        _ = compute_unbacked_bindings(fake_mode.shape_env, unbacked_idx)
    if "u15" in str(unbacked_idx):
        breakpoint()
    return unbacked_idx


# We allocate unbacked symints for int inputs since their value
# can be iteration dependent and we don't really know the value
# of the integer thus unbacked.
def unspecialize_ints_with_unbacked_symints(fake_mode, carried_inputs, bind=False):
    return tuple(
        _unspecialize_int(fake_mode, arg, bind) if isinstance(arg, int) else arg
        for arg in carried_inputs
    )


@while_loop_op.py_impl(ProxyTorchDispatchMode)
def while_loop_tracing(mode, cond_fn, body_fn, carried_inputs, additional_inputs):
    def _trace_while_loop(
        proxy_mode, while_loop_op, cond_fn, body_fn, carried_inputs, additional_inputs
    ):
        from torch._subclasses.fake_tensor import FakeTensorMode
        from torch.fx.experimental.symbolic_shapes import ShapeEnv

        # It's possible that there is no active fake tenosr mode, if we're tracing with
        # "real" mode. so we create a new one.
        fake_mode = torch._guards.detect_fake_mode()
        if fake_mode is None:
            fake_mode = FakeTensorMode(shape_env=ShapeEnv())

        # We create temporary unbacked symints for int carries to trace subgraph.
        with fake_mode.shape_env.ignore_fresh_unbacked_symbols():
            tracing_carry = unspecialize_ints_with_unbacked_symints(
                fake_mode, carried_inputs, bind=True
            )
            print("ignoring fresh unbacked symbols", tracing_carry)
        cond_graph = reenter_make_fx(cond_fn)(*tracing_carry, *additional_inputs)
        body_graph = reenter_make_fx(body_fn)(*tracing_carry, *additional_inputs)

        next_name = None
        i = 0
        while not next_name:
            candidate = f"while_loop_cond_graph_{i}"
            if hasattr(proxy_mode.tracer.root, candidate):
                i += 1
            else:
                next_name = candidate
        cond_graph_name = next_name
        body_graph_name = f"while_loop_body_graph_{i}"
        assert not hasattr(proxy_mode.tracer.root, body_graph_name)

        proxy_mode.tracer.root.register_module(cond_graph_name, cond_graph)
        proxy_mode.tracer.root.register_module(body_graph_name, body_graph)

        args = (cond_graph, body_graph, carried_inputs, additional_inputs)

        proxy_args = pytree.tree_map(proxy_mode.tracer.unwrap_proxy, args)

        out_proxy = proxy_mode.tracer.create_proxy(
            "call_function", while_loop_op, proxy_args, {}, name="while_loop"
        )

        out = while_loop_op(cond_fn, body_fn, carried_inputs, additional_inputs)
        ret = track_tensor_tree(out, out_proxy, constant=None, tracer=proxy_mode.tracer)
        return ret

    return _trace_while_loop(
        mode, while_loop_op, cond_fn, body_fn, carried_inputs, additional_inputs
    )


@while_loop_op.py_impl(FakeTensorMode)
def while_loop_fake_tensor_mode(
    mode, cond_fn, body_fn, carried_inputs, additional_inputs
):
    # body_fn return output with the same pytree and tensor meta data as carried_inputs
    # so we could just return the output after one iteration.
    #
    # We allocate unbacked symints for int outputs since their value
    # can be iteration dependent and we don't really know the value
    # of the integer thus unbacked.
    with mode:
        # Ignore the fresh_unbacked_symbols created by recursive
        # hop calls, their unbacked symbols is handled when tracing them in their
        # proxy tensor mode.
        with mode.shape_env.ignore_fresh_unbacked_symbols():
            body_output = body_fn(*carried_inputs, *additional_inputs)
        return unspecialize_ints_with_unbacked_symints(mode, body_output)


@while_loop_op.py_functionalize_impl
def while_loop_func(ctx, cond_fn, body_fn, carried_inputs, additional_inputs):
    unwrapped_carried_inputs = ctx.unwrap_tensors(carried_inputs)
    unwrapped_additional_inputs = ctx.unwrap_tensors(additional_inputs)
    unwrapped_inputs = unwrapped_carried_inputs + unwrapped_additional_inputs
    with ctx.redispatch_to_next() as m:
        functional_cond_fn = ctx.functionalize(_maybe_run_with_interpreter(cond_fn))
        functional_body_fn = ctx.functionalize(_maybe_run_with_interpreter(body_fn))
        pre_dispatch = hasattr(ctx, "mode") and ctx.mode.pre_dispatch
        for fn, fn_name in [
            (functional_cond_fn, "cond_fn"),
            (functional_body_fn, "body_fn"),
        ]:
            if _has_potential_branch_input_mutation(
                fn, unwrapped_inputs, pre_dispatch=pre_dispatch
            ):
                raise UnsupportedAliasMutationException(
                    f"torch.while_loop's {fn_name} might be modifying the input!"
                )

            if _has_potential_branch_input_alias(
                fn, unwrapped_inputs, pre_dispatch=pre_dispatch
            ):
                raise UnsupportedAliasMutationException(
                    f"torch.while_loop's {fn_name} might be aliasing the input!"
                )
        ret = while_loop_op(
            functional_cond_fn,
            functional_body_fn,
            unwrapped_carried_inputs,
            unwrapped_additional_inputs,
        )
        return ctx.wrap_tensors(ret)
