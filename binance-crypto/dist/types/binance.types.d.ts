export interface DepthStreamMessage {
    e: 'depthUpdate';
    E: number;
    s: string;
    U: number;
    u: number;
    b: [string, string][];
    a: [string, string][];
}
export interface DepthUpdateLevel {
    price: string;
    quantity: string;
}
export interface KlineStreamMessage {
    e: 'kline';
    E: number;
    s: string;
    k: {
        t: number;
        T: number;
        s: string;
        i: string;
        f: number;
        L: number;
        o: string;
        c: string;
        h: string;
        l: string;
        v: string;
        n: number;
        x: boolean;
        q: string;
        V: string;
        Q: string;
        B: string;
    };
}
export interface OHLCData {
    timestamp: number;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
}
export interface MovingAverages {
    sma7: number | null;
    sma25: number | null;
    sma99: number | null;
}
import { EventEmitter } from 'events';
import WebSocket from 'ws';
export interface BinanceWebSocketInterface {
    ee: EventEmitter;
    ws: WebSocket | null;
    ohlcData: OHLCData[];
    switchSymbol: (symbol: string, interval?: string) => void;
    processStream: (data: Buffer) => void;
    calculateMovingAverages: () => MovingAverages;
    getOHLCData: () => OHLCData[];
}
