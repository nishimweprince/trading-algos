import axios, { type AxiosInstance } from 'axios';
import type { AppConfig } from '../config.js';

export type SendSmsResult =
  | { ok: true; raw: unknown }
  | { ok: false; status: number; body: unknown };

export class PindoClient {
  private readonly http: AxiosInstance;
  private readonly senderId: string;

  constructor(private readonly config: AppConfig) {
    this.senderId = config.PINDO_SENDER_ID;
    this.http = axios.create({
      baseURL: config.PINDO_API_URL.replace(/\/?$/, '/'),
      timeout: 30_000,
      headers: { 'Content-Type': 'application/json' },
      validateStatus: () => true,
    });
  }

  async sendSms(to: string, text: string): Promise<SendSmsResult> {
    const token = this.config.PINDO_TOKEN;
    if (!token) {
      return { ok: false, status: 0, body: { error: 'PINDO_TOKEN not set' } };
    }
    const response = await this.http.post(
      '',
      { to, text, sender: this.senderId },
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (response.status >= 200 && response.status < 300) {
      return { ok: true, raw: response.data };
    }
    return { ok: false, status: response.status, body: response.data };
  }
}
