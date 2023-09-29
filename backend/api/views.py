import hashlib
import io

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.permissions import (
    IsAuthenticated,
    IsAuthenticatedOrReadOnly
)
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet, ReadOnlyModelViewSet
from rest_framework.exceptions import MethodNotAllowed
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from api.filters import IngredientFilter, RecipeFilter
from api.pagination import CustomPagination
from api.permissions import IsAuthorOrReadOnly
from api.serializers import (
    FavoriteSerializer,
    IngredientSerializer,
    RecipeCreateSerializer,
    RecipeListSerializer,
    TagSerializer
)
from recipes.models import (
    Favorite,
    Ingredient,
    RecipeIngredient,
    Recipe,
    ShoppingCart,
    Tag
)


class TagViewSet(ReadOnlyModelViewSet):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    pagination_class = None


class IngredientViewSet(ModelViewSet):
    queryset = Ingredient.objects.all()
    serializer_class = IngredientSerializer
    pagination_class = None
    permission_classes = [IsAuthorOrReadOnly]
    filter_backends = (IngredientFilter,)
    search_fields = ('^name',)

    def create(self, request, *args, **kwargs):
        raise MethodNotAllowed("POST")

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PUT")

    def partial_update(self, request, *args, **kwargs):
        raise MethodNotAllowed("PATCH")

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE")

    def finalize_response(self, request, response, *args, **kwargs):
        if (
            response.status_code != status.HTTP_405_METHOD_NOT_ALLOWED
            and request.method not in ["GET"]
        ):
            response = Response(
                {"detail": "Method Not Allowed."},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        return super().finalize_response(request, response, *args, **kwargs)


class RecipeViewSet(ModelViewSet):
    queryset = Recipe.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filter_class = RecipeFilter
    permission_classes = [IsAuthenticatedOrReadOnly]
    pagination_class = CustomPagination

    def get_serializer_class(self):
        if self.action == 'favorite' or self.action == 'cart':
            return FavoriteSerializer
        return RecipeCreateSerializer

    def get_queryset(self):
        queryset = Recipe.objects.all()
        author = self.request.user
        tags = self.request.query_params.getlist('tags', [])
        if tags:
            queryset = queryset.filter(tags__slug__in=tags).distinct()

        if self.request.GET.get('is_favorited'):
            favorite_recipes_ids = Favorite.objects.filter(
                user=author).values('recipe_id')
            return queryset.filter(pk__in=favorite_recipes_ids)

        if self.request.GET.get('is_in_shopping_cart'):
            cart_recipes_ids = ShoppingCart.objects.filter(
                user=author).values('recipe_id')
            return queryset.filter(pk__in=cart_recipes_ids)
        return queryset

    @staticmethod
    def post_list(model, user, pk):
        if model.objects.filter(user=user, recipe__id=pk).exists():
            return Response(
                {'errors': f'Рецепт уже добавлен в {model.__name__}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        recipe = get_object_or_404(Recipe, pk=pk)
        model.objects.create(user=user, recipe=recipe)
        serializer = RecipeListSerializer(recipe)
        return Response(serializer.data,
                        status=status.HTTP_201_CREATED)

    @staticmethod
    def delete_list(model, user, pk):
        obj = model.objects.filter(user=user, recipe__id=pk)
        if obj.exists():
            obj.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(
            {'errors': f'Рецепт не добавлен в {model.__name__}'},
            status=status.HTTP_400_BAD_REQUEST
        )

    @action(methods=['POST', 'DELETE'], detail=True,
            permission_classes=(IsAuthenticated,))
    def favorite(self, request, pk=None):
        try:
            recipe = Recipe.objects.get(pk=pk)
        except Recipe.DoesNotExist:
            return Response({'error': 'Recipe not found'},
                            status=status.HTTP_400_BAD_REQUEST)

        if request.method == 'POST':
            return self.post_list(Favorite, request.user, pk)
        return self.delete_list(Favorite, request.user, pk)

    @action(methods=['POST', 'DELETE'], detail=True,
            permission_classes=(IsAuthenticated,))
    def shopping_cart(self, request, pk=None):
        if request.method == 'POST':
            return self.post_list(ShoppingCart, request.user, pk)
        return self.delete_list(ShoppingCart, request.user, pk)

    @action(
        detail=False,
        permission_classes=[permissions.IsAuthenticated]
    )
    def download_shopping_cart(self, request):
        """
        Прикол конечно у вас тут с названием файла, часа 3 сидел не мог понять
        почему Content-Disposition не задает имя, хотя все уходит и приходит.
        Оказывается на фронте название файла захардкожено. Я поменял в react,
        теперь название файла задается.
        """
        ingredient_list = {}
        recipe_ingredients = RecipeIngredient.objects.filter(
            recipe__cart__user=request.user
        ).values_list(
            'ingredient__name', 'ingredient__measurement_unit', 'amount'
        )
        for item in recipe_ingredients:
            name = item[0]
            if name not in ingredient_list:
                ingredient_list[name] = {
                    'measurement_unit': item[1],
                    'amount': item[2]
                }
            else:
                ingredient_list[name]['amount'] += item[2]

        pdf_buffer = convert_pdf(ingredient_list,
                                 'Список покупок',
                                 font='Arial',
                                 font_size=12)

        if pdf_buffer:
            ingredient_list_str = str(ingredient_list)
            hash_suffix = hashlib.md5(
                ingredient_list_str.encode()).hexdigest()[:5]
            filename = f'shopping_list_{hash_suffix}.pdf'

            response = FileResponse(
                pdf_buffer,
                as_attachment=True,
                filename=filename,
            )
        else:
            response = Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return response


def convert_pdf(data, title, font, font_size):
    """Конвертирует данные в pdf-файл при помощи ReportLab."""

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)

    # Устанавливаем заголовок PDF-файла
    p.setTitle(title)

    # Регистрация шрифта
    pdfmetrics.registerFont(TTFont(font, f'./fonts/{font}.ttf'))

    # Заголовок
    p.setFont(font, font_size)
    height = 800
    p.drawString(50, height, f'{title}:')
    height -= 30

    # Тело
    p.setFont(font, font_size)
    for i, (name, info) in enumerate(data.items(), 1):
        p.drawString(75, height, (f'{i}. {name} - {info["amount"]} '
                                  f'{info["measurement_unit"]}'))
        height -= 30

    # Подпись
    current_year = timezone.now().year
    signature = f"Спасибо, что используете Foodgram Project © {current_year}"
    p.line(50, height, 550, height)
    height -= 10
    p.setFont(font, font_size - 4)
    p.drawString(50, height, signature)
    p.showPage()
    p.save()
    buffer.seek(0)

    return buffer
