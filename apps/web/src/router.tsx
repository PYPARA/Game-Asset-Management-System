import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";
import { Workbench } from "./Workbench";

const rootRoute = createRootRoute({
  component: () => <Outlet />,
  notFoundComponent: () => <Workbench />,
});

const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: Workbench,
});

const generationRoute = createRoute({getParentRoute:()=>rootRoute,path:"/generation",component:Workbench});
const generationSessionRoute = createRoute({getParentRoute:()=>rootRoute,path:"/generation/$sessionId",component:Workbench});
const routeTree = rootRoute.addChildren([workbenchRoute,generationRoute,generationSessionRoute]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
