/** @odoo-module **/

import { Component, useState, useRef, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

class ContainerLogsStream extends Component {
    static template = "saas_core.ContainerLogsStream";
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.state = useState({
            lines: [],
            connected: false,
            error: null,
            autoScroll: true,
        });
        this.logRef = useRef("logContainer");
        this.eventSource = null;
        // Bind once so addEventListener/removeEventListener use the same ref
        this._boundOnScroll = this._onScroll.bind(this);

        const { stream_url, container_name, tail } = this.props.action.context || {};
        this.streamUrl = stream_url;
        this.containerName = container_name || "Container";
        this.tail = tail || 100;

        onMounted(() => {
            this.startStream();
            const el = this.logRef.el;
            if (el) {
                el.addEventListener("scroll", this._boundOnScroll);
            }
        });
        onWillUnmount(() => {
            this.stopStream();
            const el = this.logRef.el;
            if (el) {
                el.removeEventListener("scroll", this._boundOnScroll);
            }
        });
    }

    _onScroll() {
        const el = this.logRef.el;
        if (!el) return;
        // User is "at the bottom" if within 30px of the end
        const atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 30;
        this.state.autoScroll = atBottom;
    }

    startStream() {
        this.stopStream();
        this.state.lines = [];
        this.state.error = null;
        this.state.connected = true;

        const url = `${this.streamUrl}?tail=${this.tail}`;
        this.eventSource = new EventSource(url);

        this.eventSource.onmessage = (event) => {
            const line = JSON.parse(event.data);
            this.state.lines.push(line);
            // Keep max 5000 lines in memory
            if (this.state.lines.length > 5000) {
                this.state.lines.splice(0, this.state.lines.length - 5000);
            }
            // Only auto-scroll if user is at the bottom
            if (this.state.autoScroll) {
                this.scrollToBottom();
            }
        };

        this.eventSource.addEventListener("done", () => {
            this.state.connected = false;
            this.stopStream();
        });

        this.eventSource.addEventListener("error", (event) => {
            if (event.data) {
                this.state.error = JSON.parse(event.data);
            }
            this.state.connected = false;
            this.stopStream();
        });

        this.eventSource.onerror = () => {
            this.state.connected = false;
            this.stopStream();
        };
    }

    stopStream() {
        if (this.eventSource) {
            this.eventSource.close();
            this.eventSource = null;
        }
        this.state.connected = false;
    }

    scrollToBottom() {
        requestAnimationFrame(() => {
            const el = this.logRef.el;
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
        });
    }

    onScrollToBottom() {
        this.state.autoScroll = true;
        this.scrollToBottom();
    }

    onClear() {
        this.state.lines = [];
    }

    onReconnect() {
        this.startStream();
    }

    onStop() {
        this.stopStream();
    }

    get logText() {
        return this.state.lines.join("\n");
    }
}

registry.category("actions").add("container_logs_stream", ContainerLogsStream);
