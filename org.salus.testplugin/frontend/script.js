import { Salus } from "./salus_sdk.js";

const salus = Salus.connect();
const chat = salus.channel("chat"); // ws://chat

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("chat-input");

function appendMessage(text, direction) {
    const div = document.createElement("div");
    div.classList.add("message", direction);
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

formEl.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;

    appendMessage(text, "sent");
    chat.send(text);
    inputEl.value = "";
});

chat.onMessage((message) => appendMessage(message.payload.asStr(), "received"));

// HTTP demo: calls the route the Python backend opened (GET /hello/{name}).
salus.http
    .json("GET", `/hello/${encodeURIComponent("Salus")}`)
    .then((reply) => appendMessage(`HTTP: ${reply.greeting}`, "received"))
    .catch((error) => appendMessage(`HTTP failed: ${error.message}`, "received"));
