import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReplayPage } from "@/pages/ReplayPage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ReplayPage />
    </QueryClientProvider>
  );
}
