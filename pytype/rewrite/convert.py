"""Conversion from pytd to abstract representations of Python values."""

from typing import Dict, Tuple

from pytype.rewrite.abstract import abstract
from pytype.rewrite.overlays import special_builtins


def get_module_globals(
    ctx: abstract.ContextType,
    python_version: Tuple[int, int],
) -> Dict[str, abstract.BaseValue]:
  del python_version  # not yet used
  # TODO(b/241479600): Populate from builtins.pytd.
  return {
      '__name__': ctx.ANY,
      'assert_type': special_builtins.AssertType(ctx),
      'int': abstract.BaseClass(ctx, 'int', {}),
  }
