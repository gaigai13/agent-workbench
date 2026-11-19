import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CRYSTAL_TARGET,
  DASH_COOLDOWN_SECONDS,
  DASH_DURATION_SECONDS,
  DASH_SPEED_MULTIPLIER,
  GAME_DURATION_SECONDS,
  GAME_HEIGHT,
  GAME_WIDTH,
  GameEngine,
} from '../src/game-core.mjs';

test('game stays ready until started and restarts cleanly', () => {
  const game = new GameEngine();
  game.step(0.1, { x: 1, y: 0 });
  assert.equal(game.status, 'ready');
  assert.equal(game.player.x, 72);
  game.start();
  assert.equal(game.status, 'playing');
  game.reset();
  assert.equal(game.status, 'ready');
  assert.equal(game.crystals.length, CRYSTAL_TARGET);
  assert.equal(game.timeLeft, GAME_DURATION_SECONDS);
});

test('player movement is normalized and remains inside the arena', () => {
  const game = new GameEngine();
  game.start();
  game.step(0.5, { x: 1, y: 1 });
  assert.ok(Math.abs((game.player.x - 72) - (game.player.y - GAME_HEIGHT / 2)) < 1);
  game.step(10, { x: -1, y: -1 });
  assert.ok(game.player.x >= game.player.radius);
  assert.ok(game.player.y >= game.player.radius);
  game.step(10, { x: 1, y: 1 });
  assert.ok(game.player.x <= GAME_WIDTH - game.player.radius);
  assert.ok(game.player.y <= GAME_HEIGHT - game.player.radius);
});

test('collecting every crystal wins the round', () => {
  const game = new GameEngine();
  game.start();
  game.enemy.x = GAME_WIDTH;
  game.player.speed = 500;
  for (const crystal of [...game.crystals]) {
    game.player.x = crystal.x;
    game.player.y = crystal.y;
    game.step(0.01, { x: 0, y: 0 });
  }
  assert.equal(game.crystals.length, 0);
  assert.equal(game.status, 'won');
});

test('touching the patrol ends the round', () => {
  const game = new GameEngine();
  game.start();
  game.player.x = game.enemy.x;
  game.player.y = game.enemy.y;
  game.step(0.01, { x: 0, y: 0 });
  assert.equal(game.status, 'lost');
});

test('the round ends when its timer expires', () => {
  const game = new GameEngine();
  game.start();
  game.enemy.x = GAME_WIDTH;
  game.timeLeft = 0.01;
  game.step(0.02, { x: 0, y: 0 });
  assert.equal(game.status, 'lost');
  assert.equal(game.timeLeft, 0);
});

test('dash uses the current direction, or the last direction when stationary', () => {
  const game = new GameEngine();
  game.start();
  assert.equal(game.triggerDash({ x: 0, y: 0 }), true);
  game.step(0.1, { x: 0, y: 0 });
  assert.ok(Math.abs(game.player.x - (72 + 220 * DASH_SPEED_MULTIPLIER * 0.1)) < 0.001);
  assert.equal(game.player.y, GAME_HEIGHT / 2);
  assert.ok(Math.abs(game.dashRemaining - (DASH_DURATION_SECONDS - 0.1)) < 0.001);
  assert.ok(Math.abs(game.dashCooldownRemaining - (DASH_COOLDOWN_SECONDS - 0.1)) < 0.001);

  const upward = new GameEngine();
  upward.start();
  assert.equal(upward.triggerDash({ x: 0, y: -1 }), true);
  upward.step(0.1, { x: 0, y: 0 });
  assert.equal(upward.player.x, 72);
  assert.ok(Math.abs(upward.player.y - (GAME_HEIGHT / 2 - 220 * DASH_SPEED_MULTIPLIER * 0.1)) < 0.001);
});

test('dash ignores patrol collision only while active', () => {
  const game = new GameEngine();
  game.start();
  game.enemy.x = 505;
  game.enemy.speed = 0;

  assert.equal(game.triggerDash({ x: 1, y: 0 }), true);
  game.step(0.1, { x: 0, y: 0 });
  assert.equal(game.status, 'playing');
  game.step(DASH_DURATION_SECONDS - 0.1, { x: 0, y: 0 });
  assert.equal(game.status, 'playing');
  game.player.x = game.enemy.x;
  game.player.y = game.enemy.y;
  game.step(0.001, { x: 0, y: 0 });
  assert.equal(game.status, 'lost');
});

test('patrol collision is checked immediately when dash expires', () => {
  const game = new GameEngine();
  game.start();
  game.player.x = 571.65;
  game.player.y = 13;
  game.enemy.x = 578.5;
  game.enemy.y = 13;
  game.enemy.speed = 105;
  game.enemy.direction = -1;

  assert.equal(game.triggerDash({ x: 0, y: -1 }), true);
  game.step(DASH_DURATION_SECONDS, { x: 0, y: 0 });

  assert.equal(game.status, 'lost');
});

test('dash cannot be retriggered during its three-second cooldown', () => {
  const game = new GameEngine();
  game.start();
  game.enemy.x = 500;
  game.enemy.speed = 0;
  assert.equal(game.triggerDash({ x: 1, y: 0 }), true);
  assert.equal(game.triggerDash({ x: -1, y: 0 }), false);
  game.step(DASH_DURATION_SECONDS, { x: 0, y: 0 });
  assert.equal(game.triggerDash({ x: 1, y: 0 }), false);
  game.step(DASH_COOLDOWN_SECONDS - DASH_DURATION_SECONDS, { x: 0, y: 0 });
  assert.ok(Math.abs(game.dashCooldownRemaining) < 0.001);
  assert.equal(game.triggerDash({ x: 0, y: 0 }), true);
});

test('reset clears dash and cooldown state and restores the initial direction', () => {
  const game = new GameEngine();
  game.start();
  assert.equal(game.triggerDash({ x: 0, y: -1 }), true);
  game.step(0.1, { x: 0, y: 0 });
  game.reset();

  assert.equal(game.status, 'ready');
  assert.equal(game.dashRemaining, 0);
  assert.equal(game.dashCooldownRemaining, 0);
  assert.deepEqual(game.lastDirection, { x: 1, y: 0 });
  assert.equal(game.triggerDash({ x: 0, y: 0 }), false);
  game.start();
  assert.equal(game.triggerDash({ x: 0, y: 0 }), true);
});
