import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Science from "./science/Science";
import "./styles.css";

// The shared app rail's stylesheet is served by the web server alongside the
// Chat page (one definition for all pages), plus the same fonts the Chat page
// uses. Injected at runtime: they live outside this bundle.
for (const href of [
  "/static/css/rail.css?v=20260928a",
  "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap",
]) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.appendChild(link);
}
const icon = document.createElement("link");
icon.rel = "icon";
icon.href = "/static/logo.png?v=3";
document.head.appendChild(icon);

// One bundle, two views: the Science tab is served from the same build under
// /science, the Lab under /lab. Pick the view from the path.
const isScience = location.pathname.replace(/\/$/, "").endsWith("/science");
const Root = isScience ? Science : App;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
