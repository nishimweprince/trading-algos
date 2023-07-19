import express from 'express';
import cors from 'cors';
import events from 'events';
import bodyParser from 'body-parser';
import { PORT } from './constants/environments.js';
import { binancews } from './utils/binance.js';

// INITIALIZE EXPRESS
const app = express();

// INITIALIZE EVENTS
const ee = new events();

// MIDDLEWARE
app.use(cors());
app.use(bodyParser.json({ limit: '50mb' }));

// INITIALIZE SERVER
const server = app.listen(PORT, () => {
  console.log(`Server is running on port ${PORT}`);
  ee.emit('serverStarted');
});

// INITIALIZE BINANCE WEBSOCKET
ee.on('serverStarted', () => {
  binancews.switchSymbol('BNBBTC');
});

binancews.ee.on('depth', (data) => {
  console.log(data);
});
