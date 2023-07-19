import WebSocket from 'ws';
import events from 'events';

const binancews = {
  ee: new events(),
  ws: '',
  switchSymbol: (symbol) => {
    if (binancews.ws) {
      binancews.ws.terminate();
    }
    binancews.ws = new WebSocket(
      `wss://stream.binance.com:9443/ws/${symbol.toLowerCase()}@depth`
    );
    binancews.ws.on('message', binancews.processStream);
  },
  processStream: (data) => {
    binancews.ee.emit('depth', JSON.parse(data));
  },
};

export { binancews };
