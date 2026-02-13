const loginPanel = document.getElementById("loginPanel");
const chatPanel = document.getElementById("chatPanel");
const loginForm = document.getElementById("loginForm");
const loginEmailInput = document.getElementById("loginEmail");
const loginPasswordInput = document.getElementById("loginPassword");
const loginError = document.getElementById("loginError");

const chatForm = document.getElementById("chatForm");
const userMessageInput = document.getElementById("userMessage");
const chatMessages = document.getElementById("chatMessages");
const sendButton = chatForm.querySelector("button");
const logoutButton = document.getElementById("logoutButton");
const userBadge = document.getElementById("userBadge");

const TOKEN_KEY = "gew_sales_auth_token";
const EMAIL_KEY = "gew_sales_user_email";

let conversationId = null;
let accessToken = localStorage.getItem(TOKEN_KEY) || "";
let currentEmail = localStorage.getItem(EMAIL_KEY) || "";
let authRequired = true;

function appendMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function showThinkingBubble() {
  const el = document.createElement("div");
  el.className = "msg assistant thinking";
  el.innerHTML = `<span class="dot"></span><span class="dot"></span><span class="dot"></span>`;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return el;
}

function removeThinkingBubble(el) {
  if (el && el.parentNode) {
    el.parentNode.removeChild(el);
  }
}

function setLoggedOutState() {
  accessToken = "";
  currentEmail = "";
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(EMAIL_KEY);
  loginPanel.classList.remove("hidden");
  chatPanel.classList.add("hidden");
  chatMessages.innerHTML = "";
  conversationId = null;
  loginPasswordInput.value = "";
}

function setLoggedInState(email) {
  currentEmail = email || "";
  loginPanel.classList.add("hidden");
  chatPanel.classList.remove("hidden");
  userBadge.textContent = currentEmail;
  if (!chatMessages.childElementCount) {
    appendMessage("assistant", "Hi. What can I help you figure out?");
  }
}

function setPublicDemoState() {
  authRequired = false;
  loginPanel.classList.add("hidden");
  chatPanel.classList.remove("hidden");
  userBadge.textContent = "Public demo mode";
  logoutButton.classList.add("hidden");
  if (!chatMessages.childElementCount) {
    appendMessage("assistant", "Hi. What can I help you figure out?");
  }
}

async function fetchMe() {
  if (!accessToken) return false;
  try {
    const res = await fetch("/api/me", {
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    });
    if (!res.ok) return false;
    const data = await res.json();
    currentEmail = data.email || currentEmail;
    localStorage.setItem(EMAIL_KEY, currentEmail);
    return true;
  } catch {
    return false;
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";

  const email = loginEmailInput.value.trim().toLowerCase();
  const password = loginPasswordInput.value;
  if (!email || !password) return;

  loginEmailInput.disabled = true;
  loginPasswordInput.disabled = true;

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    if (!res.ok) {
      loginError.textContent = "Invalid login details. Please try again.";
      return;
    }

    const data = await res.json();
    accessToken = data.access_token || "";
    currentEmail = data.user_email || email;
    localStorage.setItem(TOKEN_KEY, accessToken);
    localStorage.setItem(EMAIL_KEY, currentEmail);
    setLoggedInState(currentEmail);
  } catch {
    loginError.textContent = "Could not reach the server. Please try again.";
  } finally {
    loginEmailInput.disabled = false;
    loginPasswordInput.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  if (!authRequired) return;
  if (accessToken) {
    try {
      await fetch("/api/logout", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${accessToken}`,
        },
      });
    } catch {
      // ignore network logout failures in demo
    }
  }
  setLoggedOutState();
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = userMessageInput.value.trim();
  if (!message) return;
  if (authRequired && !accessToken) return;

  appendMessage("user", message);
  userMessageInput.value = "";
  userMessageInput.disabled = true;
  sendButton.disabled = true;
  const thinkingEl = showThinkingBubble();

  try {
    const payload = {
      message,
      conversation_id: conversationId,
      channel_hint: "demo_web",
      user_metadata: {
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        locale: navigator.language,
      },
      demo_mode: true,
    };

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: JSON.stringify(payload),
    });

    if (res.status === 401) {
      removeThinkingBubble(thinkingEl);
      appendMessage("assistant", "Your session expired. Please log in again.");
      setLoggedOutState();
      return;
    }

    if (!res.ok) {
      removeThinkingBubble(thinkingEl);
      appendMessage("assistant", "I hit a temporary issue. Please try again.");
      return;
    }

    const data = await res.json();
    conversationId = data.conversation_id;
    removeThinkingBubble(thinkingEl);
    appendMessage("assistant", data.assistant_reply);
  } catch {
    removeThinkingBubble(thinkingEl);
    appendMessage("assistant", "I couldn't reach the backend right now.");
  } finally {
    userMessageInput.disabled = false;
    sendButton.disabled = false;
    userMessageInput.focus();
  }
});

async function bootstrap() {
  try {
    const health = await fetch("/health");
    if (health.ok) {
      const h = await health.json();
      authRequired = Boolean(h.auth_required);
      if (!authRequired) {
        setPublicDemoState();
        return;
      }
    }
  } catch {
    // ignore health probe failures and continue with auth flow
  }

  if (!accessToken && authRequired) {
    setLoggedOutState();
    return;
  }
  const ok = await fetchMe();
  if (!ok) {
    setLoggedOutState();
    return;
  }
  setLoggedInState(currentEmail);
}

bootstrap();
