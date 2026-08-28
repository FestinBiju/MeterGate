"use client";

export const RAZORPAY_CHECKOUT_SCRIPT_ID = "razorpay-standard-checkout";
export const RAZORPAY_CHECKOUT_SCRIPT_SRC =
  "https://checkout.razorpay.com/v1/checkout.js";

export type RazorpayCheckoutSuccess = {
  razorpay_order_id: string;
  razorpay_payment_id: string;
  razorpay_signature: string;
};

export type RazorpayCheckoutFailure = {
  error?: unknown;
};

export type RazorpayCheckoutOptions = {
  key: string;
  order_id: string;
  amount: number;
  currency: string;
  name: string;
  description: string;
  handler: (response: RazorpayCheckoutSuccess) => void;
  modal: {
    confirm_close: boolean;
    ondismiss: () => void;
  };
  retry: {
    enabled: boolean;
  };
};

export type RazorpayCheckoutInstance = {
  close: () => void;
  on: (
    event: "payment.failed",
    handler: (response: RazorpayCheckoutFailure) => void,
  ) => void;
  open: () => void;
};

type RazorpayCheckoutConstructor = new (
  options: RazorpayCheckoutOptions,
) => RazorpayCheckoutInstance;

declare global {
  interface Window {
    Razorpay?: RazorpayCheckoutConstructor;
  }
}

export function isRazorpayCheckoutAvailable(): boolean {
  return typeof window !== "undefined" && typeof window.Razorpay === "function";
}

export function createRazorpayCheckout(
  options: RazorpayCheckoutOptions,
): RazorpayCheckoutInstance {
  if (!isRazorpayCheckoutAvailable() || !window.Razorpay) {
    throw new Error("Razorpay Standard Checkout is unavailable");
  }

  return new window.Razorpay(options);
}
