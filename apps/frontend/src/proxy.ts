import { NextRequest, NextResponse } from "next/server";

const SESSION_COOKIE_NAME = "frame_session";

export async function proxy(request: NextRequest) {
  const sessionToken = request.cookies.get(SESSION_COOKIE_NAME)?.value;

  if (!sessionToken) {
    return redirectToLogin(request);
  }

  try {
    const response = await fetch(new URL("/api/v1/auth/me", request.url), {
      headers: {
        cookie: request.headers.get("cookie") ?? "",
      },
      cache: "no-store",
    });

    if (response.ok) {
      return NextResponse.next();
    }
  } catch {
    // Backend doğrulanamıyorsa korunan sayfayı açma.
  }

  const redirect = redirectToLogin(request);
  redirect.cookies.delete(SESSION_COOKIE_NAME);
  return redirect;
}

function redirectToLogin(request: NextRequest) {
  const url = new URL("/login", request.url);
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/brands/:path*", "/jobs/:path*"],
};