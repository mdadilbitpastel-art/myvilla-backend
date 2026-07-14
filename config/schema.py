import strawberry

from accounts.api.mutations import AuthMutation
from accounts.api.queries import AccountQuery
from properties.api.mutations import PropertyMutation
from properties.api.queries import PropertyQuery


@strawberry.type
class Query(AccountQuery, PropertyQuery):
    @strawberry.field
    def health(self) -> str:
        return "ok"


@strawberry.type
class Mutation(AuthMutation, PropertyMutation):
    pass


schema = strawberry.Schema(query=Query, mutation=Mutation)
