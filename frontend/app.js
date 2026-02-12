const chatForm = document.getElementById("chatForm");
const userMessageInput = document.getElementById("userMessage");
const chatMessages = document.getElementById("chatMessages");

let conversationId = null;

function appendMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  chatMessages.appendChild(el);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const message = userMessageInput.value.trim();
  if (!message) return;

  appendMessage("user", message);
  userMessageInput.value = "";

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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      appendMessage("assistant", "I hit a temporary issue. Please try again.");
      return;
    }

    const data = await res.json();
    conversationId = data.conversation_id;
    appendMessage("assistant", data.assistant_reply);
  } catch (err) {
    appendMessage("assistant", "I couldn't reach the backend right now.");
  }
});

appendMessage(
  "assistant",
  "Hi. What can I help you figure out?"
);
