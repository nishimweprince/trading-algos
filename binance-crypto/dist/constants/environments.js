import dotenv from 'dotenv';
dotenv.config();
export const PORT = process.env.PORT || '3000';
export const NODE_ENV = process.env.NODE_ENV || 'development';
export const API_KEY = process.env.BINANCE_API_KEY;
export const SECRET_KEY = process.env.BINANCE_SECRET_KEY;
export const BASE_ENDPOINT_TESTNET = process.env.BINANCE_BASE_ENDPOINT_TESTNET || 'https://testnet.binance.vision';
