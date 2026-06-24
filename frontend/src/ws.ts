// Thin WebSocket wrapper for the Lab UI.
export type Handler = (msg: any) => void;

export class LabSocket {
  private ws: WebSocket | null = null;
  private handlers: Handler[] = [];
  private onOpenCbs: (() => void)[] = [];
  private queue: any[] = [];

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws`;
    this.ws = new WebSocket(url);
    this.ws.onopen = () => {
      this.queue.forEach((m) => this.ws!.send(JSON.stringify(m)));
      this.queue = [];
      this.onOpenCbs.forEach((cb) => cb());
    };
    this.ws.onmessage = (ev) => {
      let msg: any;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      this.handlers.forEach((h) => h(msg));
    };
    this.ws.onclose = () => {
      // auto-reconnect after a short delay
      setTimeout(() => this.connect(), 1500);
    };
  }

  onMessage(h: Handler) {
    this.handlers.push(h);
  }

  onOpen(cb: () => void) {
    this.onOpenCbs.push(cb);
  }

  send(msg: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    } else {
      this.queue.push(msg);
    }
  }
}
