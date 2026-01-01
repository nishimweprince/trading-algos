import express from 'express';
import cors from 'cors';
import events from 'events';
import bodyParser from 'body-parser';
import { PORT } from './constants/environments.js';
import { binancerest } from './utils/binance.js';
const app = express();
const ee = new events();
app.use(cors());
app.use(bodyParser.json({ limit: '50mb' }));
const server = app.listen(PORT, () => {
    console.log(`Server is running on port ${PORT}`);
    ee.emit('serverStarted');
});
ee.on('serverStarted', async () => {
    const fetchData = async () => {
        try {
            await binancerest.fetchKlineData('BTCUSDT');
            console.log('Kline data fetched successfully');
        }
        catch (error) {
            console.error('Failed to fetch kline data:', error);
        }
    };
    await fetchData();
    setInterval(fetchData, 60000);
});
binancerest.ee.on('ohlc', (data) => {
    const { recent100Candles, currentCandle, movingAverages, totalCandles } = data;
    console.log('==== Recent 100 Candles & Moving Averages ====');
    console.log(`Current Candle Timestamp: ${new Date(currentCandle.timestamp).toISOString()}`);
    console.log(`Current OHLC: O:${currentCandle.open} H:${currentCandle.high} L:${currentCandle.low} C:${currentCandle.close}`);
    console.log(`Current Volume: ${currentCandle.volume}`);
    console.log(`SMA-7:  ${movingAverages.sma7?.toFixed(6) ?? 'Not enough data'}`);
    console.log(`SMA-25: ${movingAverages.sma25?.toFixed(6) ?? 'Not enough data'}`);
    console.log(`SMA-99: ${movingAverages.sma99?.toFixed(6) ?? 'Not enough data'}`);
    console.log(`Total Candles Available: ${totalCandles}`);
    console.log(`Recent 100 Candles Array Length: ${recent100Candles.length}`);
    if (recent100Candles.length > 0) {
        console.log(`\nFirst 3 Candles:`);
        recent100Candles.slice(0, 3).forEach((candle, index) => {
            console.log(`  [${index}] ${new Date(candle.timestamp).toISOString()} - C:${candle.close}`);
        });
        console.log(`\nLast 3 Candles:`);
        recent100Candles.slice(-3).forEach((candle, index) => {
            const actualIndex = recent100Candles.length - 3 + index;
            console.log(`  [${actualIndex}] ${new Date(candle.timestamp).toISOString()} - C:${candle.close}`);
        });
    }
    console.log('===============================================\n This is a change');
});
