import { ServiceCatalog } from "@/components/service-catalog";
import { SystemStatus } from "@/components/system-status";

const stack = ["Next.js", "FastAPI", "PostgreSQL", "Redis"];

export default function Home() {
  return (
    <div className="relative min-h-screen overflow-hidden bg-[#080b10] text-slate-100">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[32rem] bg-[radial-gradient(circle_at_20%_0%,rgba(56,189,248,0.12),transparent_38%),radial-gradient(circle_at_85%_8%,rgba(45,212,191,0.09),transparent_28%)]"
      />

      <header className="relative mx-auto flex w-full max-w-6xl items-center justify-between px-6 py-7 lg:px-8">
        <div className="flex items-center gap-3">
          <span className="grid size-9 place-items-center rounded-xl border border-cyan-300/20 bg-cyan-300/10 text-sm font-semibold text-cyan-200 shadow-[0_0_32px_rgba(34,211,238,0.08)]">
            M
          </span>
          <span className="text-sm font-semibold tracking-wide text-white">
            MeterGate
          </span>
        </div>

        <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs font-medium text-slate-300">
          <span className="size-1.5 rounded-full bg-cyan-300" />
          Foundation
        </div>
      </header>

      <main className="relative mx-auto w-full max-w-6xl px-6 pb-20 pt-16 lg:px-8 lg:pb-28 lg:pt-24">
        <div className="grid gap-14 lg:grid-cols-[minmax(0,1.05fr)_minmax(25rem,0.95fr)] lg:items-center lg:gap-20">
          <section>
            <p className="mb-6 text-xs font-semibold uppercase tracking-[0.24em] text-cyan-300">
              Developer infrastructure · Foundation
            </p>
            <h1 className="max-w-2xl text-5xl font-semibold tracking-[-0.045em] text-white sm:text-6xl lg:text-7xl">
              MeterGate
            </h1>
            <p className="mt-7 max-w-xl text-xl leading-8 text-slate-200 sm:text-2xl sm:leading-9">
              A Razorpay-native agent storefront for paid APIs and digital
              services
            </p>
            <p className="mt-6 max-w-lg text-sm leading-6 text-slate-400 sm:text-base sm:leading-7">
              The local development foundation is in place. Infrastructure
              status below is reported directly by the FastAPI readiness endpoint.
            </p>

            <ul
              className="mt-9 flex flex-wrap gap-2"
              aria-label="Foundation stack"
            >
              {stack.map((technology) => (
                <li
                  key={technology}
                  className="rounded-full border border-white/10 bg-white/[0.035] px-3 py-1.5 text-xs text-slate-400"
                >
                  {technology}
                </li>
              ))}
            </ul>
          </section>

          <SystemStatus />
        </div>

        <ServiceCatalog />
      </main>

      <footer className="relative border-t border-white/[0.06]">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-2 px-6 py-6 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between lg:px-8">
          <span>MeterGate foundation</span>
          <span>No payment capabilities are enabled in this milestone.</span>
        </div>
      </footer>
    </div>
  );
}
