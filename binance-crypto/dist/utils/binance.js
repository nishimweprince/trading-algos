import events from 'events';
import { calculateSMA } from './indicators/movingAverages.indicator.js';
const binancerest = {
    ee: new events.EventEmitter(),
    ohlcData: [],
    fetchKlineData: async (symbol, interval = '1m', limit = 100) => {
        try {
            const baseUrl = process.env.BINANCE_BASE_REST_API || 'https://api.binance.com';
            const url = `${baseUrl}/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`;
            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            binancerest.ohlcData = data.map((kline) => ({
                timestamp: kline[0],
                open: parseFloat(kline[1]),
                high: parseFloat(kline[2]),
                low: parseFloat(kline[3]),
                close: parseFloat(kline[4]),
                volume: parseFloat(kline[5])
            }));
            const movingAverages = binancerest.calculateMovingAverages();
            const recent100Candles = binancerest.getOHLCData();
            const currentCandle = recent100Candles[recent100Candles.length - 1];
            binancerest.ee.emit('ohlc', {
                recent100Candles,
                currentCandle,
                movingAverages,
                totalCandles: binancerest.ohlcData.length
            });
        }
        catch (error) {
            console.error('Error fetching kline data:', error);
            throw error;
        }
    },
    calculateMovingAverages: () => {
        const closePrices = binancerest.ohlcData.map(d => d.close);
        return {
            sma7: closePrices.length >= 7 ? calculateSMA(closePrices, 7).slice(-1)[0] : null,
            sma25: closePrices.length >= 25 ? calculateSMA(closePrices, 25).slice(-1)[0] : null,
            sma99: closePrices.length >= 99 ? calculateSMA(closePrices, 99).slice(-1)[0] : null,
        };
    },
    getOHLCData: () => {
        return [...binancerest.ohlcData];
    }
};
export { binancerest };
