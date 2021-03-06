# Lint as: python3
# Copyright 2019 DeepMind Technologies Limited. All Rights Reserved.
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
# ==============================================================================
"""Tests for haiku._src.recurrent."""

import itertools as it

from absl.testing import absltest
from absl.testing import parameterized
from haiku._src import base
from haiku._src import basic
from haiku._src import recurrent
from haiku._src import test_utils
import jax
import jax.nn
import jax.numpy as jnp
import numpy as np
import tree


class RecurrentTest(parameterized.TestCase):

  def test_add_batch(self):
    sample_tree = dict(
        a=[jnp.zeros([]), jnp.zeros([1])],
        b=jnp.zeros([1, 1]),
    )
    batch_size = 2
    out = recurrent.add_batch(sample_tree, batch_size)
    tree.assert_same_structure(sample_tree, out)
    flat_in = tree.flatten(sample_tree)
    flat_out = tree.flatten(out)
    for in_array, out_array in zip(flat_in, flat_out):
      self.assertEqual(out_array.shape[0], batch_size)
      self.assertEqual(out_array.shape[1:], in_array.shape)

  # These two tests assume that the core takes argument hidden_size, and the
  # output is a single tensor with the same size as hidden_size.
  # They should be generalized when new cores are added.
  @parameterized.parameters(
      *it.product((recurrent.dynamic_unroll, recurrent.static_unroll),
                  (recurrent.VanillaRNN, recurrent.LSTM, recurrent.GRU)))
  def test_core_unroll_unbatched(self, unroll, core_cls):
    def net(seqs):
      # seqs is [T, F]
      core = core_cls(hidden_size=4)
      outs, state = unroll(core, seqs, core.initial_state(batch_size=None))
      return outs, state

    seq = make_sequence([8, 1])
    init_fn, apply_fn = base.transform(net)
    params = init_fn(jax.random.PRNGKey(428), seq)
    out, _ = apply_fn(params, seq)
    self.assertEqual(out.shape, (8, 4))

  @parameterized.parameters(
      *it.product((recurrent.dynamic_unroll, recurrent.static_unroll),
                  (recurrent.VanillaRNN, recurrent.LSTM, recurrent.GRU)))
  def test_core_unroll_batched(self, unroll, core_cls):
    def net(seqs):
      # seqs is [T, B, F]
      core = core_cls(hidden_size=4)
      batch_size = seqs.shape[1]
      outs, state = unroll(core, seqs, core.initial_state(batch_size))
      return outs, state

    seqs = make_sequence([4, 8, 1])
    init_fn, apply_fn = base.transform(net)
    params = init_fn(jax.random.PRNGKey(428), seqs)
    out, _ = apply_fn(params, seqs)
    self.assertEqual(out.shape, (4, 8, 4))


class LSTMTest(absltest.TestCase):

  @test_utils.transform_and_run
  def test_lstm_raises(self):
    core = recurrent.LSTM(4)
    with self.assertRaisesRegex(ValueError, "rank-1 or rank-2"):
      core(jnp.zeros([]), core.initial_state(None))

    with self.assertRaisesRegex(ValueError, "rank-1 or rank-2"):
      expanded_state = tree.map_structure(lambda x: jnp.expand_dims(x, 0),
                                          core.initial_state(1))
      core(jnp.zeros([1, 1, 1]), expanded_state)


class ResetCoreTest(parameterized.TestCase):

  @parameterized.parameters(recurrent.dynamic_unroll, recurrent.static_unroll)
  def test_resetting(self, unroll):
    def net(seqs, should_reset):
      # seqs is [T, B, F].
      core = recurrent.LSTM(hidden_size=4)
      reset_core = recurrent.ResetCore(core)
      batch_size = seqs.shape[1]

      # Statically unroll, collecting states.
      core_outs, core_states = static_unroll_with_states(
          core, seqs, core.initial_state(batch_size))
      reset_outs, reset_states = static_unroll_with_states(
          reset_core, (seqs, should_reset),
          reset_core.initial_state(batch_size))

      # Unroll without access to intermediate states.
      dynamic_core_outs, dynamic_core_state = unroll(
          core, seqs, core.initial_state(batch_size))
      dynamic_reset_outs, dynamic_reset_state = unroll(
          reset_core, (seqs, should_reset),
          reset_core.initial_state(batch_size))

      return dict(
          core_outs=core_outs,
          core_states=core_states,
          reset_outs=reset_outs,
          reset_states=reset_states,
          dynamic_core_outs=dynamic_core_outs,
          dynamic_core_state=dynamic_core_state,
          dynamic_reset_outs=dynamic_reset_outs,
          dynamic_reset_state=dynamic_reset_state,
      )

    batch_size = 4
    # Reset one batch element on the second step.
    resets = [[False] * batch_size, [True] + [False] * (batch_size - 1)]
    resets = np.asarray(resets)

    # Each sequence is the same input twice.
    seqs = make_sequence([batch_size, 1])
    seqs = np.stack([seqs, seqs], axis=0)

    init_fn, apply_fn = base.transform(net)
    params = init_fn(jax.random.PRNGKey(428), seqs, resets)
    result = apply_fn(params, seqs, resets)

    # Verify dynamic and static unroll gave same outs and final states.
    np.testing.assert_allclose(
        result["core_outs"], result["dynamic_core_outs"], rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(
        result["reset_outs"],
        result["dynamic_reset_outs"],
        rtol=1e-6,
        atol=1e-6)
    for s, d in zip(result["core_states"], result["dynamic_core_state"]):
      np.testing.assert_allclose(s[-1], d, rtol=1e-6, atol=1e-6)
    for s, d in zip(result["reset_states"], result["dynamic_reset_state"]):
      np.testing.assert_allclose(s[-1], d, rtol=1e-6, atol=1e-6)

    # Now, test resetting behavior on static outputs.
    core_outs = result["core_outs"]
    core_states = result["core_states"]
    reset_outs = result["reset_outs"]
    reset_states = result["reset_states"]

    # If no reset occurred, the reset core should do nothing.
    np.testing.assert_allclose(
        core_outs[0], reset_outs[0], rtol=1e-6, atol=1e-6)
    for cs, rs in zip(core_states, reset_states):
      np.testing.assert_allclose(cs[0], rs[0], rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(
        core_outs[1, 1:], reset_outs[1, 1:], rtol=1e-6, atol=1e-6)
    for cs, rs in zip(core_states, reset_states):
      np.testing.assert_allclose(cs[1, 1:], rs[1, 1:], rtol=1e-6, atol=1e-6)

    # Check that the reset occurred where specified.
    np.testing.assert_allclose(
        core_outs[0, 0], reset_outs[1, 0], rtol=1e-6, atol=1e-6)
    for cs, rs in zip(core_states, reset_states):
      np.testing.assert_allclose(cs[0, 0], rs[1, 0], rtol=1e-6, atol=1e-6)


class DeepRNNTest(parameterized.TestCase):

  def test_only_callables(self):

    def net(x):
      # x is [B, F].
      core = recurrent.DeepRNN([jnp.tanh, jnp.square])
      initial_state = core.initial_state(x.shape[0])
      out, next_state = core(x, initial_state)
      return dict(out=out, next_state=next_state, initial_state=initial_state)

    data = make_sequence([4, 3])
    init_fn, apply_fn = base.transform(net)
    params = init_fn(None, data)
    result = apply_fn(params, data)

    np.testing.assert_allclose(
        result["out"], np.square(np.tanh(data)), rtol=1e-4)
    self.assertEqual(result["next_state"], tuple())
    self.assertEqual(result["initial_state"], tuple())

  def test_connection_and_shapes(self):

    def net(x):
      # x is [B, F].
      core = recurrent.DeepRNN([
          recurrent.VanillaRNN(hidden_size=3),
          basic.Linear(2),
          jax.nn.relu,
          recurrent.VanillaRNN(hidden_size=5),
          jax.nn.relu,
      ])
      initial_state = core.initial_state(x.shape[0])
      out, next_state = core(x, initial_state)

      return dict(out=out, next_state=next_state, initial_state=initial_state)

    batch_size = 4
    data = make_sequence([batch_size, 3])
    init_fn, apply_fn = base.transform(net)
    params = init_fn(jax.random.PRNGKey(428), data)
    result = apply_fn(params, data)

    self.assertEqual(result["out"].shape, (batch_size, 5))
    # Verifies that at least last layer of relu is applied.
    self.assertTrue(np.all(result["out"] >= np.zeros([batch_size, 5])))

    self.assertLen(result["next_state"], 2)
    self.assertEqual(result["initial_state"][0].shape, (batch_size, 3))
    self.assertEqual(result["initial_state"][1].shape, (batch_size, 5))

    self.assertLen(result["initial_state"], 2)
    np.testing.assert_allclose(result["initial_state"][0],
                               jnp.zeros([batch_size, 3]))
    np.testing.assert_allclose(result["initial_state"][1],
                               jnp.zeros([batch_size, 5]))

  def test_skip_connections(self):

    def net(x):
      # x is [B, F].
      core = recurrent.deep_rnn_with_skip_connections([
          recurrent.VanillaRNN(hidden_size=3),
          recurrent.VanillaRNN(hidden_size=5),
      ])
      initial_state = core.initial_state(x.shape[0])
      out, _ = core(x, initial_state)
      return out

    batch_size = 4
    data = make_sequence([batch_size, 3])
    init_fn, apply_fn = base.transform(net)
    params = init_fn(jax.random.PRNGKey(428), data)
    result = apply_fn(params, data)

    self.assertEqual(result.shape, (batch_size, 8))
    # Previous tests test the correctness of state handling.

  @test_utils.transform_and_run
  def test_skip_validation(self):
    with self.assertRaisesRegex(ValueError, "skip_connections requires"):
      recurrent.deep_rnn_with_skip_connections([jax.nn.relu])


def make_sequence(shape):
  # Skips 0 for meaningful multiplicative interactions.
  return np.arange(1, np.product(shape) + 1, dtype=np.float32).reshape(shape)


def static_unroll_with_states(core, inputs, state):
  outs = []
  states = []
  steps = tree.flatten(inputs)[0].shape[0]
  for i in range(steps):
    step_input = tree.map_structure(lambda x: x[i], inputs)  # pylint: disable=cell-var-from-loop
    out, state = core(step_input, state)
    outs.append(out)
    states.append(state)

  outs = jnp.stack(outs, axis=0)
  states = tree.map_structure(lambda *a: jnp.stack(a, axis=0), *states)
  return outs, states

if __name__ == "__main__":
  absltest.main()
