import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { watchLooks } from "./lib/away";
import { connect } from "./lib/live";
import { applyLook } from "./lib/prefs";
import "./styles/tokens.css";
import "./styles/base.css";
import "./components/ui.css";
import "./reading/reading.css";

applyLook();
connect();
watchLooks();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
