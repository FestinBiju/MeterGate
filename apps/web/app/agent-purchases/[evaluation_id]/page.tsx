import { AccountAuth } from "@/components/account-auth";
import { AgentPurchaseHandoff } from "@/components/agent-purchase-handoff";
import { AccountSessionProvider } from "@/components/account-session";

export default async function AgentPurchasePage({
  params,
}: {
  params: Promise<{ evaluation_id: string }>;
}) {
  const { evaluation_id: evaluationId } = await params;
  return (
    <main className="min-h-screen bg-[#080b10] px-6 py-12 text-slate-100">
      <div className="mx-auto max-w-4xl">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300">Trusted browser boundary</p>
        <h1 className="mt-3 text-4xl font-semibold text-white">Agent purchase handoff</h1>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-400">
          The agent may prepare commerce evidence, but only the owning human can approve with a passkey and complete Razorpay Test Mode Checkout.
        </p>
        <p className="mt-3 break-all font-mono text-xs text-slate-500">{evaluationId}</p>
        <AccountSessionProvider>
          <AccountAuth />
          <AgentPurchaseHandoff evaluationId={evaluationId} />
        </AccountSessionProvider>
      </div>
    </main>
  );
}
