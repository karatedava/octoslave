import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import Science from "./Science";
import "./styles.css";

// One bundle, two views: the Science tab is served from the same build under
// /science, the Lab under /lab. Pick the view from the path.
const isScience = location.pathname.replace(/\/$/, "").endsWith("/science");
const Root = isScience ? Science : App;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
