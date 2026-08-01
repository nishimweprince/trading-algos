import { stripHtml, formatMessageWithSubject } from '../src/channels/message.util';

describe('message.util', () => {
  it('strips HTML tags', () => {
    expect(stripHtml('<b>BUY</b> EUR/USD')).toBe('BUY EUR/USD');
  });

  it('formats message with subject', () => {
    expect(formatMessageWithSubject('Alert', 'Hello')).toBe(
      'Subject: Alert\n\nHello',
    );
  });

  it('returns message unchanged when subject is empty', () => {
    expect(formatMessageWithSubject(undefined, 'Hello')).toBe('Hello');
  });
});
