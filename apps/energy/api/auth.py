"""Obtention du jeton d'API. Mêmes protections que la page de connexion (verrouillage après échecs)."""
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView


class TokenThrottle(AnonRateThrottle):
    scope = "token"


class CredentialsSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, write_only=True, style={"input_type": "password"})


def _key(username, request):
    ip = request.META.get("REMOTE_ADDR", "")
    return f"api-login-fail:{username.lower()}:{ip}"


class ObtainToken(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [TokenThrottle]

    @extend_schema(tags=["Authentification"], summary="Obtenir un jeton d'API", request=CredentialsSerializer,
                   responses={200: inline_serializer("Token", {"token": serializers.CharField()})})
    def post(self, request):
        ser = CredentialsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        username, password = ser.validated_data["username"], ser.validated_data["password"]
        key = _key(username, request)
        if cache.get(key, 0) >= settings.LOGIN_MAX_ATTEMPTS:
            return Response({"detail": "Trop de tentatives échouées. Réessayez dans quelques minutes."},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        user = authenticate(request=request, username=username, password=password)
        if user is None or not user.is_active:
            cache.set(key, cache.get(key, 0) + 1, settings.LOGIN_LOCK_SECONDS)
            return Response({"detail": "Identifiants incorrects."}, status=status.HTTP_401_UNAUTHORIZED)
        cache.delete(key)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})
