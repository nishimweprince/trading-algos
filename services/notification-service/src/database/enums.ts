export enum DeliveryChannel {
  TELEGRAM = 'TELEGRAM',
  EMAIL = 'EMAIL',
  SMS = 'SMS',
  WHATSAPP = 'WHATSAPP',
}

export enum DeliveryStatus {
  pending = 'pending',
  sent = 'sent',
  failed = 'failed',
  delivered = 'delivered',
  read = 'read',
}
