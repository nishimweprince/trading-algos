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
import { EventEmitter } from 'events';
import WebSocket from 'ws';
export interface BinanceWebSocketInterface {
    ee: EventEmitter;
    ws: WebSocket | null;
    switchSymbol: (symbol: string) => void;
    processStream: (data: Buffer) => void;
}
