const tg = window.Telegram?.WebApp;
const app = document.querySelector("#app");
let model = null;
let selectedGame = "tic_tac_toe";
let selectedId = null;
let polling = null;

if (tg) {
  tg.ready();
  tg.expand();
  if (tg.setHeaderColor) tg.setHeaderColor("secondary_bg_color");
}

const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const api = async (path, body = {}) => {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type":"application/json", "X-Telegram-Init-Data": tg?.initData || ""},
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ok:false, error:"Не удалось прочитать ответ игры."}));
  if (!response.ok || !data.ok) throw new Error(data.error || "Игра временно недоступна.");
  return data.result;
};

function statusLabel(game) {
  if (game.status === "pending") return game.can_accept ? "вас ждут" : "приглашение";
  if (game.status === "active") return game.your_turn ? "ваш ход" : "ход соперника";
  if (game.status === "finished") return game.winner === "draw" ? "ничья" : game.winner === "you" ? "вы выиграли" : "соперник выиграл";
  return game.status === "declined" ? "отклонено" : game.status === "cancelled" ? "завершена" : "закрыто";
}
function currentGame() { return model?.games.find((game) => game.id === selectedId) || model?.games.find((game) => game.status === "active" || game.can_accept) || null; }
function renderNotice(message = "", isError = false) { return message ? `<p class="notice ${isError ? "error" : ""}">${escapeHtml(message)}</p>` : ""; }
function profileView() {
  if (model.profile?.telegram_username) return "";
  return `<section class="panel"><h2>Играйте без игрового ника</h2><p>У вашего Telegram-аккаунта нет публичного @username, поэтому вас нельзя найти через поиск. Это не мешает: создайте ссылку-приглашение и отправьте её контакту.</p></section>`;
}
function lobbyView() {
  const tiles = model.catalog.map((game) => `<button class="game-card ${selectedGame === game.id ? "selected" : ""}" data-kind="${game.id}"><strong>${escapeHtml(game.title)}</strong><span>${game.players} игрока · по очереди</span></button>`).join("");
  const games = model.games.map((game) => `<button class="game-row" data-game="${game.id}"><span><strong>${escapeHtml(game.title)} · ${escapeHtml(game.opponent.nickname)}</strong><small>${escapeHtml(statusLabel(game))}</small></span><b class="badge">${escapeHtml(statusLabel(game))}</b></button>`).join("");
  const links = (model.share_links || []).map((link) => `<div class="share-link"><strong>${escapeHtml(link.title)}</strong><input value="${escapeHtml(link.url)}" readonly aria-label="Ссылка-приглашение"><div class="actions"><button class="button ghost" data-copy="${escapeHtml(link.url)}">Копировать</button><button class="button" data-share="${escapeHtml(link.url)}">Отправить</button></div></div>`).join("");
  return `<section class="panel"><h2>Новая партия</h2><p>Выберите игру. Можно пригласить человека по его реальному Telegram @username или отправить ссылку.</p><div class="game-grid">${tiles}</div><form class="stack" data-form="invite"><label>Telegram @username соперника<input name="username" maxlength="33" autocomplete="off" autocapitalize="none" placeholder="например, @ola_krakow" required></label><button class="button">Пригласить по @username</button></form><div class="or">или</div><button class="button ghost" data-action="create-link">Создать ссылку-приглашение</button></section>${links ? `<section class="panel"><h2>Ожидают по ссылке</h2><p>Ссылка действует 7 дней и будет использована первым, кому вы её отправите.</p>${links}</section>` : ""}${games ? `<section class="panel"><h2>Ваши партии</h2><div class="game-list">${games}</div></section>` : ""}`;
}
function boardView(game) {
  if (!game) return "";
  const board = game.state.board || [];
  const prompt = game.status === "pending" ? (game.can_accept ? "Вас пригласили в игру" : "Ждём ответ соперника") : game.status === "active" ? (game.your_turn ? "Ваш ход" : "Ход соперника") : statusLabel(game);
  const cells = board.map((mark, index) => `<button class="cell ${mark === "X" ? "x" : mark === "O" ? "o" : ""}" data-move="${index}" ${(!game.your_turn || mark || game.status !== "active") ? "disabled" : ""} aria-label="Клетка ${index + 1}">${mark || ""}</button>`).join("");
  const pendingActions = game.can_accept ? `<button class="button" data-action="accept">Принять</button><button class="button ghost" data-action="decline">Отклонить</button>` : "";
  const activeActions = game.status === "active" ? `<button class="button danger" data-action="resign">Сдаться</button><button class="button ghost" data-action="finish">Завершить игру</button>` : "";
  const actions = `${pendingActions}${activeActions}`;
  return `<section class="panel"><div class="board-head"><div><h2>${escapeHtml(game.title)}</h2><p>Вы: ${escapeHtml(game.you.nickname)} · ${game.you.marker} &nbsp; Соперник: ${escapeHtml(game.opponent.nickname)} · ${game.opponent.marker}</p></div><span class="turn">${escapeHtml(prompt)}</span></div><div class="board">${cells}</div><div class="actions">${actions}<button class="button ghost" data-action="lobby">К списку игр</button></div></section>`;
}
function render(message = "", isError = false) {
  if (!tg) { app.innerHTML = `<section class="panel"><h2>Откройте игру из Telegram</h2><p>Так мы безопасно узнаем игроков и сохраним партию.</p></section>`; return; }
  if (!model) { app.innerHTML = `<section class="panel"><h2>Не удалось открыть игру</h2>${renderNotice(message || "Попробуйте открыть игру из чата с ботом.", true)}</section>`; return; }
  const game = currentGame();
  app.innerHTML = `<header class="masthead"><div><div class="eyebrow">Tutorlaing · вдвоём</div><h1>Игровой стол</h1></div>${model?.profile?.telegram_username ? `<span class="nick">@${escapeHtml(model.profile.telegram_username)}</span>` : ""}</header>${renderNotice(message, isError)}${profileView()}${boardView(game)}${lobbyView()}`;
  app.querySelectorAll("[data-kind]").forEach((button) => button.addEventListener("click", () => { selectedGame = button.dataset.kind; selectedId = null; render(); }));
  app.querySelectorAll("[data-game]").forEach((button) => button.addEventListener("click", () => { selectedId = button.dataset.game; render(); }));
  app.querySelectorAll("[data-move]").forEach((button) => button.addEventListener("click", () => perform("/games/api/move", {game_id: currentGame().id, position: Number(button.dataset.move)})));
  app.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => { const action = button.dataset.action; if (action === "lobby") { selectedId = null; render(); } else if (action === "create-link") { createLink(); } else perform(`/games/api/${action}`, {game_id: currentGame().id}); }));
  app.querySelectorAll("[data-copy]").forEach((button) => button.addEventListener("click", () => copyLink(button.dataset.copy || "")));
  app.querySelectorAll("[data-share]").forEach((button) => button.addEventListener("click", () => shareLink(button.dataset.share || "")));
  const invite = app.querySelector('[data-form="invite"]');
  if (invite) invite.addEventListener("submit", (event) => { event.preventDefault(); perform("/games/api/invitations", {kind: selectedGame, username: new FormData(invite).get("username")}); });
}
async function refresh(message = "", isError = false) { try { model = await api("/games/api/state"); render(message, isError); } catch (error) { render(error.message, true); } }
async function perform(path, body) { try { await api(path, body); const message = path.endsWith("/move") ? "Ход отправлен." : path.endsWith("/resign") ? "Вы сдались. Партия завершена." : path.endsWith("/finish") ? "Партия завершена без победителя." : "Готово."; await refresh(message); } catch (error) { render(error.message, true); } }
async function createLink() { try { await api("/games/api/link-invitations", {kind: selectedGame}); await refresh("Ссылка готова: скопируйте её или отправьте прямо из Telegram."); } catch (error) { render(error.message, true); } }
async function copyLink(url) { try { await navigator.clipboard.writeText(url); render("Ссылка скопирована."); } catch (_) { render("Не удалось скопировать ссылку. Нажмите и удерживайте поле со ссылкой.", true); } }
function shareLink(url) { const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent("Сыграем в крестики-нолики в Tutorlaing?")}`; if (tg?.openTelegramLink) tg.openTelegramLink(shareUrl); else copyLink(url); }
async function bootstrap() { const joinToken = new URLSearchParams(window.location.search).get("join") || ""; if (joinToken) { try { await api("/games/api/claim-link", {token: joinToken}); await refresh("Приглашение получено. Примите партию, когда будете готовы."); return; } catch (error) { await refresh(error.message, true); return; } } await refresh(); }
bootstrap();
polling = window.setInterval(() => { if (document.visibilityState === "visible" && model) refresh(); }, 3000);
window.addEventListener("beforeunload", () => window.clearInterval(polling));
