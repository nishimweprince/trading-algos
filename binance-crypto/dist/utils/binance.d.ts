import events from 'events';
import { OHLCData, MovingAverages } from '../types/binance.types.js';
interface BinanceRestInterface {
    ee: events.EventEmitter;
    ohlcData: OHLCData[];
    fetchKlineData: (symbol: string, interval?: string, limit?: number) => Promise<void>;
    calculateMovingAverages: () => MovingAverages;
    getOHLCData: () => OHLCData[];
}
declare const binancerest: BinanceRestInterface;
export { binancerest };
