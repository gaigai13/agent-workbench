export const GAME_WIDTH = 720;
export const GAME_HEIGHT = 440;
export const GAME_DURATION_SECONDS = 60;
export const CRYSTAL_TARGET = 5;
export const DASH_DURATION_SECONDS = 0.35;
export const DASH_COOLDOWN_SECONDS = 3;
export const DASH_SPEED_MULTIPLIER = 2.8;

const INITIAL_CRYSTALS = [
  { x: 190, y: 105 },
  { x: 340, y: 335 },
  { x: 525, y: 110 },
  { x: 610, y: 330 },
  { x: 365, y: 205 },
];

export class GameEngine {
  constructor() {
    this.reset();
  }

  reset() {
    this.status = 'ready';
    this.timeLeft = GAME_DURATION_SECONDS;
    this.player = { x: 72, y: GAME_HEIGHT / 2, radius: 13, speed: 220 };
    this.lastDirection = { x: 1, y: 0 };
    this.dashDirection = null;
    this.dashRemaining = 0;
    this.dashCooldownRemaining = 0;
    this.enemy = { x: 505, y: GAME_HEIGHT / 2, radius: 17, speed: 105, minX: 430, maxX: 655, direction: 1 };
    this.crystals = INITIAL_CRYSTALS.map((crystal) => ({ ...crystal, radius: 10 }));
  }

  start() {
    if (this.status === 'ready') this.status = 'playing';
  }

  triggerDash(input = { x: 0, y: 0 }) {
    if (this.status !== 'playing' || this.dashRemaining > 0 || this.dashCooldownRemaining > 0) return false;
    const x = Number.isFinite(input.x) ? input.x : 0;
    const y = Number.isFinite(input.y) ? input.y : 0;
    const magnitude = Math.hypot(x, y);
    if (magnitude > 0) this.lastDirection = { x: x / magnitude, y: y / magnitude };
    this.dashDirection = { ...this.lastDirection };
    this.dashRemaining = DASH_DURATION_SECONDS;
    this.dashCooldownRemaining = DASH_COOLDOWN_SECONDS;
    return true;
  }

  step(deltaSeconds, input = { x: 0, y: 0 }) {
    if (this.status !== 'playing' || !Number.isFinite(deltaSeconds) || deltaSeconds <= 0) return;

    const dx = Number.isFinite(input.x) ? input.x : 0;
    const dy = Number.isFinite(input.y) ? input.y : 0;
    const magnitude = Math.hypot(dx, dy) || 1;
    const scale = magnitude > 1 ? 1 / magnitude : 1;
    if (Math.hypot(dx, dy) > 0) {
      this.lastDirection = { x: dx / Math.hypot(dx, dy), y: dy / Math.hypot(dx, dy) };
    }
    const wasDashing = this.dashRemaining > 0;
    const dashTime = Math.min(deltaSeconds, this.dashRemaining);
    const unroundedNormalTime = deltaSeconds - dashTime;
    const normalTime = unroundedNormalTime < 1e-9 ? 0 : unroundedNormalTime;
    const dashDirection = wasDashing ? this.dashDirection : { x: dx * scale, y: dy * scale };
    const normalDirection = { x: dx * scale, y: dy * scale };
    this.player.x = this.#clamp(
      this.player.x + dashDirection.x * this.player.speed * DASH_SPEED_MULTIPLIER * dashTime
        + normalDirection.x * this.player.speed * normalTime,
      this.player.radius,
      GAME_WIDTH - this.player.radius,
    );
    this.player.y = this.#clamp(
      this.player.y + dashDirection.y * this.player.speed * DASH_SPEED_MULTIPLIER * dashTime
        + normalDirection.y * this.player.speed * normalTime,
      this.player.radius,
      GAME_HEIGHT - this.player.radius,
    );
    this.dashRemaining = Math.max(0, this.dashRemaining - deltaSeconds);
    if (this.dashRemaining < 1e-9) this.dashRemaining = 0;
    this.dashCooldownRemaining = Math.max(0, this.dashCooldownRemaining - deltaSeconds);
    if (this.dashCooldownRemaining < 1e-9) this.dashCooldownRemaining = 0;

    this.enemy.x += this.enemy.direction * this.enemy.speed * deltaSeconds;
    if (this.enemy.x >= this.enemy.maxX) {
      this.enemy.x = this.enemy.maxX;
      this.enemy.direction = -1;
    } else if (this.enemy.x <= this.enemy.minX) {
      this.enemy.x = this.enemy.minX;
      this.enemy.direction = 1;
    }

    this.timeLeft = Math.max(0, this.timeLeft - deltaSeconds);
    if (this.dashRemaining === 0 && this.#overlaps(this.player, this.enemy)) {
      this.status = 'lost';
      return;
    }

    this.crystals = this.crystals.filter((crystal) => !this.#overlaps(this.player, crystal));
    if (this.crystals.length === 0) {
      this.status = 'won';
    } else if (this.timeLeft === 0) {
      this.status = 'lost';
    }
  }

  #clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  #overlaps(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y) < a.radius + b.radius;
  }
}
