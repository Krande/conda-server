import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-lg py-16 text-center">
      <h1 className="text-3xl font-semibold tracking-tight">404</h1>
      <p className="mt-2 text-slate-600 dark:text-slate-400">
        That page doesn't exist.
      </p>
      <div className="mt-6">
        <Link to="/">
          <Button variant="secondary">Back to home</Button>
        </Link>
      </div>
    </div>
  );
}
