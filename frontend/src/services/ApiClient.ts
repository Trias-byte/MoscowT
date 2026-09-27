import type { z } from 'zod';

interface RequestOptions {
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export class ApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly idempotent = false,
  ) {}

  url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  request<T>(path: string, schema: z.ZodType<T>, options: RequestOptions = {}): Promise<T> {
    const { body, signal, headers } = options;
    return this.send(path, schema, {
      method: body === undefined ? 'GET' : 'POST',
      signal,
      headers: {
        'Content-Type': 'application/json',
        ...(this.idempotent && body !== undefined
          ? { 'Idempotency-Key': crypto.randomUUID() }
          : {}),
        ...headers,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  }

  upload<T>(path: string, file: File, schema: z.ZodType<T>): Promise<T> {
    return this.send(path, schema, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
  }

  private async send<T>(path: string, schema: z.ZodType<T>, options: RequestInit): Promise<T> {
    const response = await fetch(this.url(path), options);
    let value: unknown;
    try {
      value = await response.json();
    } catch {
      throw new Error(`Сервер вернул некорректный ответ (HTTP ${response.status}).`);
    }
    if (!response.ok) {
      throw new Error(
        platformErrorMessage(value && typeof value === 'object' ? value : {}, response.status),
      );
    }
    return schema.parse(value);
  }
}

export function platformErrorMessage(
  value: { error?: { message?: string }; detail?: unknown },
  status: number,
) {
  if (value.error?.message) return value.error.message;
  if (typeof value.detail === 'string') return value.detail;
  if (Array.isArray(value.detail)) {
    const messages = value.detail.map((item) => {
      const message = typeof item?.msg === 'string' ? item.msg : '';
      if (message.includes('Time must be aligned to a Moscow calendar hour'))
        return 'Данные почасовые. Выберите время с нулевыми минутами, например 23:00.';
      if (message.includes('Expected a non-empty half-open interval'))
        return 'Конец периода должен быть позже начала.';
      if (item?.loc?.includes('time_range'))
        return 'Проверьте дату и время начала и окончания периода.';
      return 'Проверьте заполненные поля формы.';
    });
    if (messages.length) return [...new Set(messages)].join(' ');
  }
  return `Ошибка запроса (HTTP ${status}). Повторите попытку.`;
}
