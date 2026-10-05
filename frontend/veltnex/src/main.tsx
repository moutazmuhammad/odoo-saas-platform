import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { ToastProvider } from "./context/ToastContext";
import { InstancesProvider } from "./context/InstancesContext";
import "./index.css";
import { initializeLanguage, loadLanguage } from "./i18n";

initializeLanguage();

async function bootstrap() {
  await loadLanguage();
  const { default: App } = await import("./App");
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <BrowserRouter>
        <ToastProvider>
          <AuthProvider>
            <InstancesProvider>
              <App />
            </InstancesProvider>
          </AuthProvider>
        </ToastProvider>
      </BrowserRouter>
    </React.StrictMode>
  );
}

void bootstrap();
