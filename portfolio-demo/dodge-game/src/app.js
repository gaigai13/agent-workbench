import { GAME_HEIGHT, GAME_WIDTH, GameEngine } from './game-core.mjs';

const canvas = document.querySelector('#game-canvas');
const ctx = canvas.getContext('2d');
const engine = new GameEngine();
const pressedKeys = new Set();
const pressedDirections = new Set();
const startButton = document.querySelector('#start-button');
const resetButton = document.querySelector('#reset-button');
const statusText = document.querySelector('#game-status');
const timeText = document.querySelector('#time-left');
const crystalText = document.querySelector('#crystal-count');
const dashStatusText = document.querySelector('#dash-status');
const dashButton = document.querySelector('#dash-button');

const keyDirections = {
  ArrowUp: [0, -1],
  w: [0, -1],
  ArrowDown: [0, 1],
  s: [0, 1],
  ArrowLeft: [-1, 0],
  a: [-1, 0],
  ArrowRight: [1, 0],
  d: [1, 0],
};

const directionVectors = {
  up: [0, -1],
  down: [0, 1],
  left: [-1, 0],
  right: [1, 0],
};

function currentInput() {
  const input = { x: 0, y: 0 };
  for (const key of pressedKeys) {
    const vector = keyDirections[key];
    if (vector) {
      input.x += vector[0];
      input.y += vector[1];
    }
  }
  for (const direction of pressedDirections) {
    const vector = directionVectors[direction];
    if (vector) {
      input.x += vector[0];
      input.y += vector[1];
    }
  }
  return input;
}

function draw() {
  ctx.clearRect(0, 0, GAME_WIDTH, GAME_HEIGHT);
  ctx.fillStyle = '#101b32';
  ctx.fillRect(0, 0, GAME_WIDTH, GAME_HEIGHT);

  ctx.strokeStyle = 'rgba(156, 180, 220, 0.08)';
  ctx.lineWidth = 1;
  for (let x = 0; x <= GAME_WIDTH; x += 40) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, GAME_HEIGHT);
    ctx.stroke();
  }
  for (let y = 0; y <= GAME_HEIGHT; y += 40) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(GAME_WIDTH, y);
    ctx.stroke();
  }

  for (const crystal of engine.crystals) {
    ctx.beginPath();
    ctx.fillStyle = '#7bf0d1';
    ctx.shadowColor = '#7bf0d1';
    ctx.shadowBlur = 18;
    ctx.arc(crystal.x, crystal.y, crystal.radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  ctx.beginPath();
  ctx.fillStyle = '#ff6d89';
  ctx.arc(engine.enemy.x, engine.enemy.y, engine.enemy.radius, 0, Math.PI * 2);
  ctx.fill();

  ctx.beginPath();
  ctx.fillStyle = '#a99bff';
  ctx.shadowColor = '#8d7aff';
  ctx.shadowBlur = 18;
  ctx.arc(engine.player.x, engine.player.y, engine.player.radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.shadowBlur = 0;
  if (engine.dashRemaining > 0) {
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(123, 240, 209, 0.9)';
    ctx.lineWidth = 3;
    ctx.arc(engine.player.x, engine.player.y, engine.player.radius + 7, 0, Math.PI * 2);
    ctx.stroke();
  }
}

function renderStatus() {
  const time = Math.ceil(engine.timeLeft);
  timeText.textContent = `${String(Math.floor(time / 60)).padStart(2, '0')}:${String(time % 60).padStart(2, '0')}`;
  crystalText.textContent = `${5 - engine.crystals.length} / 5`;

  const messages = {
    ready: '准备好了吗？收集 5 颗晶体，避开巡逻障碍。',
    playing: '进行中：用方向键或 WASD 移动。',
    won: '成功！晶体全部收集。',
    lost: engine.timeLeft === 0 ? '时间到！再试一次。' : '被障碍碰到了，再试一次。',
  };
  statusText.textContent = messages[engine.status];
  startButton.hidden = engine.status === 'playing';
  startButton.textContent = engine.status === 'ready' ? '开始游戏' : '再玩一局';
  if (engine.status !== 'playing') {
    dashStatusText.textContent = '就绪 · Space';
    dashButton.disabled = true;
    dashButton.textContent = '冲刺';
    return;
  }
  dashButton.disabled = engine.dashCooldownRemaining > 0;
  dashButton.textContent = engine.dashRemaining > 0 ? '冲刺中' : '冲刺';
  if (engine.dashRemaining > 0) {
    dashStatusText.textContent = `冲刺中 · ${engine.dashRemaining.toFixed(1)}s`;
  } else if (engine.dashCooldownRemaining > 0) {
    dashStatusText.textContent = `冷却 · ${engine.dashCooldownRemaining.toFixed(1)}s`;
  } else {
    dashStatusText.textContent = '可用 · Space';
  }
}

startButton.addEventListener('click', () => {
  if (engine.status === 'won' || engine.status === 'lost') engine.reset();
  engine.start();
  renderStatus();
});

resetButton.addEventListener('click', () => {
  engine.reset();
  pressedKeys.clear();
  pressedDirections.clear();
  renderStatus();
  draw();
});

window.addEventListener('keydown', (event) => {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === ' ' || key === 'Spacebar') {
    event.preventDefault();
    engine.triggerDash(currentInput());
  }
  if (keyDirections[key]) {
    if (key.startsWith('Arrow')) event.preventDefault();
    pressedKeys.add(key);
  }
});

window.addEventListener('keyup', (event) => {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  pressedKeys.delete(key);
});
window.addEventListener('blur', () => pressedKeys.clear());

document.querySelectorAll('[data-direction]').forEach((button) => {
  const direction = button.dataset.direction;
  button.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    pressedDirections.add(direction);
  });
  const release = () => pressedDirections.delete(direction);
  button.addEventListener('pointerup', release);
  button.addEventListener('pointercancel', release);
  button.addEventListener('lostpointercapture', release);
});

dashButton.addEventListener('pointerdown', (event) => {
  event.preventDefault();
  engine.triggerDash(currentInput());
  renderStatus();
});
dashButton.addEventListener('click', () => {
  engine.triggerDash(currentInput());
  renderStatus();
});

let previousFrame = performance.now();
function frame(now) {
  const deltaSeconds = Math.min((now - previousFrame) / 1000, 0.05);
  previousFrame = now;
  engine.step(deltaSeconds, currentInput());
  renderStatus();
  draw();
  requestAnimationFrame(frame);
}

renderStatus();
draw();
requestAnimationFrame(frame);
