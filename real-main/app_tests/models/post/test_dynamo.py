import decimal
import logging
from uuid import uuid4

import pendulum
import pytest

from app.models.post import exceptions
from app.models.post.dynamo import PostDynamo
from app.models.post.enums import PostStatus


@pytest.fixture
def post_dynamo(dynamo_client):
    yield PostDynamo(dynamo_client)


def test_post_does_not_exist(post_dynamo):
    post_id = 'my-post-id'
    resp = post_dynamo.get_post(post_id)
    assert resp is None


def test_post_add_and_delete(post_dynamo):
    post_id = 'my-post-id'
    user_id = 'my-user-id'

    # add the post
    transact = post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text='lore ipsum')
    post_dynamo.client.transact_write_items([transact])

    # post exists now
    resp = post_dynamo.get_post(post_id)
    assert resp['postId'] == post_id

    # check strongly_consistent kwarg accepted
    resp = post_dynamo.get_post(post_id, strongly_consistent=True)
    assert resp['postId'] == post_id

    # delete the post, check no longer exists
    post_dynamo.delete_post(post_id)
    assert post_dynamo.get_post(post_id) is None


def test_transact_add_pending_post_sans_options(post_dynamo):
    user_id = 'pbuid'
    post_id = 'pid'
    posted_at = pendulum.now('utc')
    post_type = 'ptype'

    # add the post
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id, post_type, posted_at=posted_at)]
    post_dynamo.client.transact_write_items(transacts)

    # retrieve post, check format
    posted_at_str = posted_at.to_iso8601_string()
    post_item = post_dynamo.get_post(post_id)
    assert post_item == {
        'schemaVersion': 3,
        'partitionKey': 'post/pid',
        'sortKey': '-',
        'gsiA2PartitionKey': 'post/pbuid',
        'gsiA2SortKey': f'{PostStatus.PENDING}/{posted_at_str}',
        'postedByUserId': 'pbuid',
        'postId': 'pid',
        'postType': 'ptype',
        'postStatus': PostStatus.PENDING,
        'postedAt': posted_at_str,
    }


def test_transact_add_pending_post_with_options(post_dynamo):
    user_id = 'pbuid'
    post_id = 'pid'
    post_type = 'ptype'
    album_id = 'aid'
    posted_at = pendulum.now('utc')
    expires_at = pendulum.now('utc')
    text = 'lore @ipsum'
    text_tags = [{'tag': '@ipsum', 'userId': 'uid'}]

    transacts = [
        post_dynamo.transact_add_pending_post(
            user_id,
            post_id,
            post_type,
            posted_at=posted_at,
            expires_at=expires_at,
            text=text,
            text_tags=text_tags,
            comments_disabled=True,
            likes_disabled=False,
            sharing_disabled=False,
            verification_hidden=True,
            album_id=album_id,
            set_as_user_photo=True,
        )
    ]
    post_dynamo.client.transact_write_items(transacts)

    # retrieve post, check format
    posted_at_str = posted_at.to_iso8601_string()
    expires_at_str = expires_at.to_iso8601_string()
    post_item = post_dynamo.get_post(post_id)
    assert post_item == {
        'schemaVersion': 3,
        'partitionKey': 'post/pid',
        'sortKey': '-',
        'gsiA2PartitionKey': 'post/pbuid',
        'gsiA2SortKey': PostStatus.PENDING + '/' + posted_at_str,
        'postedByUserId': 'pbuid',
        'postId': 'pid',
        'postType': 'ptype',
        'postStatus': PostStatus.PENDING,
        'albumId': 'aid',
        'postedAt': posted_at_str,
        'expiresAt': expires_at_str,
        'gsiA1PartitionKey': 'post/pbuid',
        'gsiA1SortKey': PostStatus.PENDING + '/' + expires_at_str,
        'gsiK1PartitionKey': 'post/' + expires_at_str[:10],
        'gsiK1SortKey': expires_at_str[11:-1],
        'gsiK3PartitionKey': 'post/aid',
        'gsiK3SortKey': -1,
        'text': text,
        'textTags': text_tags,
        'commentsDisabled': True,
        'likesDisabled': False,
        'sharingDisabled': False,
        'verificationHidden': True,
        'setAsUserPhoto': True,
    }


def test_transact_add_post_already_exists(post_dynamo):
    user_id = 'uid'
    post_id = 'pid'
    post_type = 'ptype'

    # add the post
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id, post_type)]
    post_dynamo.client.transact_write_items(transacts)

    # try to add it again
    with pytest.raises(post_dynamo.client.exceptions.TransactionCanceledException):
        post_dynamo.client.transact_write_items(transacts)


def test_generate_posts_by_user(post_dynamo):
    user_id = 'uid'

    # add & complete a post by another user as bait (shouldn't show up in our upcoming queries)
    transacts = [post_dynamo.transact_add_pending_post('other-uid', 'pidX', 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post('pidX')
    transacts = [post_dynamo.transact_set_post_status(post_item, PostStatus.COMPLETED)]
    post_dynamo.client.transact_write_items(transacts)

    # test generate no posts
    assert list(post_dynamo.generate_posts_by_user(user_id)) == []

    # we add a post
    post_id = 'pid'
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)

    # should see if if we generate all statues, but not for COMPLETED status only
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id)] == [post_id]
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id, completed=True)] == []
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id, completed=False)] == [post_id]

    # complete the post
    transacts = [post_dynamo.transact_set_post_status(post_item, PostStatus.COMPLETED)]
    post_dynamo.client.transact_write_items(transacts)

    # should see if if we generate all statues, and for COMPLETED status only
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id)] == [post_id]
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id, completed=True)] == [post_id]
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id, completed=False)] == []

    # we add another post
    post_id_2 = 'pid2'
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id_2, 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)

    # check genertaion
    post_ids = [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id)]
    assert sorted(post_ids) == ['pid', 'pid2']
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id, completed=True)] == [post_id]
    assert [p['postId'] for p in post_dynamo.generate_posts_by_user(user_id, completed=False)] == [post_id_2]


def test_transact_set_post_status(post_dynamo):
    post_id = 'my-post-id'
    user_id = 'my-user-id'
    keys_that_change = ('postStatus', 'gsiA2SortKey')

    # add a post, verify starts pending
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)
    org_post_item = post_dynamo.get_post(post_id)
    assert org_post_item['postStatus'] == PostStatus.PENDING

    # set post status without specifying an original post id
    new_status = 'yup'
    transacts = [post_dynamo.transact_set_post_status(org_post_item, new_status)]
    post_dynamo.client.transact_write_items(transacts)
    new_post_item = post_dynamo.get_post(post_id)
    assert new_post_item.pop('postStatus') == new_status
    assert new_post_item.pop('gsiA2SortKey').startswith(new_status + '/')
    assert {**new_post_item, **{k: org_post_item[k] for k in keys_that_change}} == org_post_item

    # set post status *with* specifying an original post id
    new_status = 'new new'
    original_post_id = 'opid'
    transacts = [
        post_dynamo.transact_set_post_status(new_post_item, new_status, original_post_id=original_post_id)
    ]
    post_dynamo.client.transact_write_items(transacts)
    new_post_item = post_dynamo.get_post(post_id)
    assert new_post_item.pop('postStatus') == new_status
    assert new_post_item.pop('gsiA2SortKey').startswith(new_status + '/')
    assert new_post_item.pop('originalPostId') == original_post_id
    assert {**new_post_item, **{k: org_post_item[k] for k in keys_that_change}} == org_post_item

    # verify the album_rank cannot be specified since we're not in an album
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_post_status(org_post_item, PostStatus.COMPLETED, album_rank=0.5)
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_post_status(org_post_item, PostStatus.ARCHIVED, album_rank=0.5)


def test_transact_set_post_status_with_expires_at_and_album_id(post_dynamo):
    post_id = 'my-post-id'
    user_id = 'my-user-id'

    # add a post, verify starts pending
    expires_at = pendulum.now('utc') + pendulum.duration(days=1)
    post_dynamo.client.transact_write_items(
        [
            post_dynamo.transact_add_pending_post(
                user_id, post_id, 'ptype', text='l', expires_at=expires_at, album_id='aid'
            ),
        ]
    )
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.PENDING

    new_status = PostStatus.DELETING
    transacts = [post_dynamo.transact_set_post_status(post_item, new_status)]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == new_status
    assert post_item['gsiA2SortKey'].startswith(new_status + '/')
    assert post_item['gsiA1SortKey'].startswith(new_status + '/')


def test_transact_set_post_status_COMPLETED_clears_set_as_user_photo(post_dynamo):
    post_id = 'my-post-id'
    user_id = 'my-user-id'

    # add a post, verify stating state
    post_dynamo.client.transact_write_items(
        [post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', set_as_user_photo=True)]
    )
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.PENDING
    assert post_item['setAsUserPhoto'] is True

    # change to PROCESSING, should not delete setAsUserPhoto
    transacts = [post_dynamo.transact_set_post_status(post_item, PostStatus.PROCESSING)]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.PROCESSING
    assert post_item['setAsUserPhoto'] is True

    # complete the post, should delete setAsUserPhoto
    transacts = [post_dynamo.transact_set_post_status(post_item, PostStatus.COMPLETED)]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.COMPLETED
    assert 'setAsUserPhoto' not in post_item


def test_transact_set_post_status_album_rank_handled_correctly_to_and_from_COMPLETED_in_album(post_dynamo):
    post_id = 'my-post-id'
    user_id = 'my-user-id'

    # add a post, verify starts pending
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text='l', album_id='aid')]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.PENDING
    assert post_item['gsiK3SortKey'] == -1

    # verify the album_rank is required when transitioning to COMPLETED
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_post_status(post_item, PostStatus.COMPLETED)

    # successfully transition to COMPLETED
    transacts = [post_dynamo.transact_set_post_status(post_item, PostStatus.COMPLETED, album_rank=0.5)]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.COMPLETED
    assert post_item['gsiK3SortKey'] == 0.5

    # verify the album_rank is required to not be present when transitioning out of COMPLETED
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_post_status(post_item, PostStatus.ARCHIVED, album_rank=0.33)

    # successfully transition out of COMPLETED
    transacts = [post_dynamo.transact_set_post_status(post_item, PostStatus.ARCHIVED)]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.ARCHIVED
    assert post_item['gsiK3SortKey'] == -1


def test_set_checksum(post_dynamo):
    post_id = 'pid'
    posted_at_str = pendulum.now('utc').to_iso8601_string()
    checksum = 'check this sum!'

    # no support for deleting a checksum
    with pytest.raises(AssertionError):
        post_dynamo.set_checksum(post_id, posted_at_str, None)

    # can't set for post that doesnt exist
    with pytest.raises(post_dynamo.client.exceptions.ConditionalCheckFailedException):
        post_dynamo.set_checksum(post_id, posted_at_str, checksum)

    # create the post
    transacts = [post_dynamo.transact_add_pending_post('uid', post_id, 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)

    # check starting state
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postId'] == post_id
    assert 'checksum' not in post_item
    assert 'gsiK2PartitionKey' not in post_item
    assert 'gsiK2SortKey' not in post_item
    posted_at_str = post_item['postedAt']

    # set the checksum, check result
    new_item = post_dynamo.set_checksum(post_id, posted_at_str, checksum)
    assert new_item.pop('checksum') == 'check this sum!'
    assert new_item.pop('gsiK2PartitionKey') == 'postChecksum/check this sum!'
    assert new_item.pop('gsiK2SortKey') == posted_at_str
    assert new_item == post_item


def test_get_first_with_checksum(post_dynamo):
    checksum = 'shaken, not checked'

    # no post
    assert post_dynamo.get_first_with_checksum(checksum) is None

    # one post
    post_id_1 = 'pid'
    posted_at_1 = pendulum.now('utc')
    post_dynamo.client.transact_write_items(
        [
            post_dynamo.transact_add_pending_post(
                'uid', post_id_1, 'ptype', text='lore ipsum', posted_at=posted_at_1
            )
        ]
    )
    posted_at_str_1 = posted_at_1.to_iso8601_string()
    post_dynamo.set_checksum(post_id_1, posted_at_str_1, checksum)
    assert post_dynamo.get_first_with_checksum(checksum) == post_id_1

    # two media, we should get the one with earliest postedAt
    post_id_2 = 'pid2'
    posted_at_2 = pendulum.now('utc')
    post_dynamo.client.transact_write_items(
        [
            post_dynamo.transact_add_pending_post(
                'uid', post_id_2, 'ptype', text='lore ipsum', posted_at=posted_at_2
            )
        ]
    )
    posted_at_str_2 = posted_at_2.to_iso8601_string()
    post_dynamo.set_checksum(post_id_2, posted_at_str_2, checksum)
    assert post_dynamo.get_first_with_checksum(checksum) == post_id_1


def test_post_set_is_verified(post_dynamo):
    post_id = 'pid'

    # can't set for post that doesnt exist
    with pytest.raises(post_dynamo.client.exceptions.ConditionalCheckFailedException):
        post_dynamo.set_is_verified(post_id, True)

    # create the post
    transacts = [post_dynamo.transact_add_pending_post('uid', post_id, 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)

    # verify starting state
    assert 'isVerified' not in post_dynamo.get_post(post_id)

    # change the value, verify
    post_item = post_dynamo.set_is_verified(post_id, True)
    assert post_item['isVerified'] is True
    assert post_dynamo.get_post(post_id)['isVerified'] is True

    # change the value, verify
    post_item = post_dynamo.set_is_verified(post_id, False)
    assert post_item['isVerified'] is False
    assert post_dynamo.get_post(post_id)['isVerified'] is False


def test_batch_get_posted_by_user_ids_not_found(post_dynamo):
    post_id = 'my-post-id'
    resp = post_dynamo.batch_get_posted_by_user_ids([post_id])
    assert resp == []


def test_batch_get_posted_by_user_ids(post_dynamo):
    user_id_1 = 'my-user-id-1'
    user_id_2 = 'my-user-id-2'
    post_id_1 = 'my-post-id-1'
    post_id_2 = 'my-post-id-2'
    post_id_3 = 'my-post-id-3'
    post_id_4 = 'my-post-id-4'

    # first user adds two posts, second user adds one post, leaves one post DNE
    transacts = [
        post_dynamo.transact_add_pending_post(user_id_1, post_id_1, 'ptype', text='lore ipsum'),
        post_dynamo.transact_add_pending_post(user_id_1, post_id_2, 'ptype', text='lore ipsum'),
        post_dynamo.transact_add_pending_post(user_id_2, post_id_3, 'ptype', text='lore ipsum'),
    ]
    post_dynamo.client.transact_write_items(transacts)

    resp = post_dynamo.batch_get_posted_by_user_ids([post_id_1, post_id_2, post_id_3, post_id_4])
    assert sorted(resp) == [user_id_1, user_id_1, user_id_2]


def test_increment_viewed_by_count(post_dynamo):
    # verify can't increment for post that doesnt exist
    post_id = 'post-id'
    with pytest.raises(exceptions.PostDoesNotExist):
        post_dynamo.increment_viewed_by_count(post_id)

    # create the post
    transacts = [post_dynamo.transact_add_pending_post('uid', post_id, 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)

    # verify it has no view count
    post_item = post_dynamo.get_post(post_id)
    assert post_item.get('viewedByCount', 0) == 0

    # record a view
    post_item = post_dynamo.increment_viewed_by_count(post_id)
    assert post_item['postId'] == post_id
    assert post_item['viewedByCount'] == 1

    # verify it really got the view count
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postId'] == post_id
    assert post_item['viewedByCount'] == 1

    # record another view
    post_item = post_dynamo.increment_viewed_by_count(post_id)
    assert post_item['postId'] == post_id
    assert post_item['viewedByCount'] == 2

    # verify it really got the view count
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postId'] == post_id
    assert post_item['viewedByCount'] == 2


def test_set_expires_at_matches_creating_story_directly(post_dynamo):
    # create a post with a lifetime, then delete it
    user_id = 'uid'
    post_id = 'post-id'
    text = 'lore ipsum'
    expires_at = pendulum.now('utc') + pendulum.duration(hours=1)
    transacts = [
        post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text=text, expires_at=expires_at)
    ]
    post_dynamo.client.transact_write_items(transacts)

    org_post_item = post_dynamo.get_post(post_id)
    assert org_post_item['postId'] == post_id
    assert org_post_item['expiresAt'] == expires_at.to_iso8601_string()

    # delete it from the DB
    post_dynamo.client.delete_item({'partitionKey': f'post/{post_id}', 'sortKey': '-'})

    # now add it to the DB, without a lifetime
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text=text)]
    post_dynamo.client.transact_write_items(transacts)
    new_post_item = post_dynamo.get_post(post_id)
    assert new_post_item['postId'] == post_id
    assert 'expiresAt' not in new_post_item

    # set the expires at, now the post items should match, except for postedAt timestamp
    new_post_item = post_dynamo.set_expires_at(new_post_item, expires_at)
    new_post_item['postedAt'] = org_post_item['postedAt']
    new_post_item['gsiA2SortKey'] = org_post_item['gsiA2SortKey']
    assert new_post_item == org_post_item


def test_remove_expires_at_matches_creating_story_directly(post_dynamo):
    # create a post with without lifetime, then delete it
    user_id = 'uid'
    post_id = 'post-id'
    text = 'lore ipsum'
    transacts = [post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text=text)]
    post_dynamo.client.transact_write_items(transacts)
    org_post_item = post_dynamo.get_post(post_id)
    assert org_post_item['postId'] == post_id
    assert 'expiresAt' not in org_post_item

    # delete it from the DB
    post_dynamo.client.delete_item({'partitionKey': f'post/{post_id}', 'sortKey': '-'})

    # now add it to the DB, with a lifetime
    expires_at = pendulum.now('utc') + pendulum.duration(hours=1)
    transacts = [
        post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text=text, expires_at=expires_at)
    ]
    post_dynamo.client.transact_write_items(transacts)
    new_post_item = post_dynamo.get_post(post_id)
    assert new_post_item['postId'] == post_id
    assert new_post_item['expiresAt'] == expires_at.to_iso8601_string()

    # remove the expires at, now the post items should match
    new_post_item = post_dynamo.remove_expires_at(post_id)
    new_post_item['postedAt'] = org_post_item['postedAt']
    new_post_item['gsiA2SortKey'] = org_post_item['gsiA2SortKey']
    assert new_post_item == org_post_item


def test_get_next_completed_post_to_expire_no_posts(post_dynamo):
    user_id = 'user-id'
    post = post_dynamo.get_next_completed_post_to_expire(user_id)
    assert post is None


def test_get_next_completed_post_to_expire_one_post(post_dynamo):
    user_id = 'user-id'
    post_id_1 = 'post-id-1'
    expires_at = pendulum.now('utc') + pendulum.duration(hours=1)

    transacts = [
        post_dynamo.transact_add_pending_post(user_id, post_id_1, 'ptype', text='t', expires_at=expires_at)
    ]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id_1)
    post_dynamo.client.transact_write_items(
        [post_dynamo.transact_set_post_status(post_item, PostStatus.COMPLETED)]
    )

    assert post_dynamo.get_next_completed_post_to_expire(user_id)['postId'] == post_id_1


def test_get_next_completed_post_to_expire_two_posts(post_dynamo):
    user_id = 'user-id'
    post_id_1, post_id_2 = 'post-id-1', 'post-id-2'
    now = pendulum.now('utc')
    expires_at_1, expires_at_2 = now + pendulum.duration(days=1), now + pendulum.duration(hours=12)

    # add those posts
    transacts = [
        post_dynamo.transact_add_pending_post(user_id, post_id_1, 'ptype', text='t', expires_at=expires_at_1),
        post_dynamo.transact_add_pending_post(user_id, post_id_2, 'ptype', text='t', expires_at=expires_at_2),
    ]
    post_dynamo.client.transact_write_items(transacts)
    post1 = post_dynamo.get_post(post_id_1)
    post2 = post_dynamo.get_post(post_id_2)

    # check niether of them show up
    assert post_dynamo.get_next_completed_post_to_expire(user_id) is None

    # complete one of them, check
    post_dynamo.client.transact_write_items([post_dynamo.transact_set_post_status(post1, PostStatus.COMPLETED)])
    post1 = post_dynamo.get_post(post_id_1)
    assert post_dynamo.get_next_completed_post_to_expire(user_id) == post1
    assert post_dynamo.get_next_completed_post_to_expire(user_id, exclude_post_id=post_id_1) is None

    # complete the other, check
    post_dynamo.client.transact_write_items([post_dynamo.transact_set_post_status(post2, PostStatus.COMPLETED)])
    post2 = post_dynamo.get_post(post_id_2)
    assert post_dynamo.get_next_completed_post_to_expire(user_id) == post2
    assert post_dynamo.get_next_completed_post_to_expire(user_id, exclude_post_id=post_id_1) == post2
    assert post_dynamo.get_next_completed_post_to_expire(user_id, exclude_post_id=post_id_2) == post1


def test_set_no_values(post_dynamo):
    with pytest.raises(AssertionError, match='edit'):
        post_dynamo.set('post-id')


def test_set_text(post_dynamo):
    # create a post with some text
    text = 'for shiz'
    transacts = [post_dynamo.transact_add_pending_post('uidA', 'pid1', 'ptype', text=text, text_tags=[])]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post('pid1')
    assert post_item['text'] == text
    assert post_item['textTags'] == []

    # edit that text
    new_text = 'over the rainbow'
    post_item = post_dynamo.set('pid1', text=new_text, text_tags=[])
    assert post_item['text'] == new_text
    assert post_item['textTags'] == []
    post_item = post_dynamo.get_post('pid1')
    assert post_item['text'] == new_text
    assert post_item['textTags'] == []

    # edit that text with a tag
    new_text = 'over the @rainbow'
    new_text_tags = [{'tag': '@rainbow', 'userId': 'tagged-uid'}]
    post_item = post_dynamo.set('pid1', text=new_text, text_tags=new_text_tags)
    assert post_item['text'] == new_text
    assert post_item['textTags'] == new_text_tags
    post_item = post_dynamo.get_post('pid1')
    assert post_item['text'] == new_text
    assert post_item['textTags'] == new_text_tags

    # delete that text
    post_item = post_dynamo.set('pid1', text='')
    assert 'text' not in post_item
    assert 'textTags' not in post_item
    post_item = post_dynamo.get_post('pid1')
    assert 'text' not in post_item
    assert 'textTags' not in post_item


def test_set_comments_disabled(post_dynamo):
    # create a post with some text, media objects
    transacts = [post_dynamo.transact_add_pending_post('uidA', 'pid1', 'ptype', text='t')]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post('pid1')
    assert 'commentsDisabled' not in post_item

    # edit it back and forth
    post_item = post_dynamo.set('pid1', comments_disabled=True)
    assert post_item['commentsDisabled'] is True
    post_item = post_dynamo.set('pid1', comments_disabled=False)
    assert post_item['commentsDisabled'] is False

    # double check the value stuck
    post_item = post_dynamo.get_post('pid1')
    assert post_item['commentsDisabled'] is False


def test_set_likes_disabled(post_dynamo):
    # create a post with some text, media objects
    transacts = [post_dynamo.transact_add_pending_post('uidA', 'pid1', 'ptype', text='t')]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post('pid1')
    assert 'likesDisabled' not in post_item

    # edit it back and forth
    post_item = post_dynamo.set('pid1', likes_disabled=True)
    assert post_item['likesDisabled'] is True
    post_item = post_dynamo.set('pid1', likes_disabled=False)
    assert post_item['likesDisabled'] is False

    # double check the value stuck
    post_item = post_dynamo.get_post('pid1')
    assert post_item['likesDisabled'] is False


def test_set_sharing_disabled(post_dynamo):
    # create a post with some text, media objects
    transacts = [post_dynamo.transact_add_pending_post('uidA', 'pid1', 'ptype', text='t')]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post('pid1')
    assert 'sharingDisabled' not in post_item

    # edit it back and forth
    post_item = post_dynamo.set('pid1', sharing_disabled=True)
    assert post_item['sharingDisabled'] is True
    post_item = post_dynamo.set('pid1', sharing_disabled=False)
    assert post_item['sharingDisabled'] is False

    # double check the value stuck
    post_item = post_dynamo.get_post('pid1')
    assert post_item['sharingDisabled'] is False


def test_set_verification_hidden(post_dynamo):
    # create a post with some text, media objects
    transacts = [post_dynamo.transact_add_pending_post('uidA', 'pid1', 'ptype', text='t')]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post('pid1')
    assert 'verificationHidden' not in post_item

    # edit it back and forth
    post_item = post_dynamo.set('pid1', verification_hidden=True)
    assert post_item['verificationHidden'] is True
    post_item = post_dynamo.set('pid1', verification_hidden=False)
    assert post_item['verificationHidden'] is False

    # double check the value stuck
    post_item = post_dynamo.get_post('pid1')
    assert post_item['verificationHidden'] is False


def test_generate_expired_post_pks_by_day(post_dynamo):
    # add three posts, two that expire on the same day, and one that never expires, and complete them all
    now = pendulum.now('utc')
    approx_hours_till_noon_tomorrow = 36 - now.time().hour
    lifetime_1 = pendulum.duration(hours=approx_hours_till_noon_tomorrow)
    lifetime_2 = pendulum.duration(hours=(approx_hours_till_noon_tomorrow + 6))
    expires_at_1 = now + lifetime_1
    expires_at_2 = now + lifetime_2

    transacts = [
        post_dynamo.transact_add_pending_post('uidA', 'post-id-1', 'ptype', text='no', expires_at=expires_at_1),
        post_dynamo.transact_add_pending_post('uidA', 'post-id-2', 'ptype', text='me', expires_at=expires_at_2),
        post_dynamo.transact_add_pending_post('uidA', 'post-id-3', 'ptype', text='digas'),
    ]
    post_dynamo.client.transact_write_items(transacts)
    post1 = post_dynamo.get_post('post-id-1')
    post2 = post_dynamo.get_post('post-id-2')
    post3 = post_dynamo.get_post('post-id-3')

    post_dynamo.client.transact_write_items([post_dynamo.transact_set_post_status(post1, PostStatus.COMPLETED)])
    post_dynamo.client.transact_write_items([post_dynamo.transact_set_post_status(post2, PostStatus.COMPLETED)])
    post_dynamo.client.transact_write_items([post_dynamo.transact_set_post_status(post3, PostStatus.COMPLETED)])

    expires_at_1 = pendulum.parse(post1['expiresAt'])
    expires_at_date = expires_at_1.date()
    cut_off_time = expires_at_1.time()

    # before any of the posts expire - checks exclusive cut off
    expired_posts = list(post_dynamo.generate_expired_post_pks_by_day(expires_at_date, cut_off_time))
    assert expired_posts == []

    # one of the posts has expired
    cut_off_time = (expires_at_1 + pendulum.duration(hours=1)).time()
    expired_posts = list(post_dynamo.generate_expired_post_pks_by_day(expires_at_date, cut_off_time))
    assert len(expired_posts) == 1
    assert expired_posts[0]['partitionKey'] == post1['partitionKey']
    assert expired_posts[0]['sortKey'] == post1['sortKey']

    # both of posts have expired
    cut_off_time = (expires_at_1 + pendulum.duration(hours=7)).time()
    expired_posts = list(post_dynamo.generate_expired_post_pks_by_day(expires_at_date, cut_off_time))
    assert len(expired_posts) == 2
    assert expired_posts[0]['partitionKey'] == post1['partitionKey']
    assert expired_posts[0]['sortKey'] == post1['sortKey']
    assert expired_posts[1]['partitionKey'] == post2['partitionKey']
    assert expired_posts[1]['sortKey'] == post2['sortKey']

    # check the whole day
    expired_posts = list(post_dynamo.generate_expired_post_pks_by_day(expires_at_date))
    assert len(expired_posts) == 2
    assert expired_posts[0]['partitionKey'] == post1['partitionKey']
    assert expired_posts[0]['sortKey'] == post1['sortKey']
    assert expired_posts[1]['partitionKey'] == post2['partitionKey']
    assert expired_posts[1]['sortKey'] == post2['sortKey']


def test_generate_expired_post_pks_with_scan(post_dynamo):
    # add four posts, one that expires a week ago, one that expires yesterday
    # and one that expires today, and one that doesnt expire
    now = pendulum.now('utc')
    week_ago = now - pendulum.duration(days=7)
    yesterday = now - pendulum.duration(days=1)
    lifetime = pendulum.duration(seconds=1)

    gen_transact = post_dynamo.transact_add_pending_post
    transacts = [
        gen_transact('u', 'p1', 'ptype', text='no', posted_at=week_ago, expires_at=(week_ago + lifetime)),
        gen_transact('u', 'p2', 'ptype', text='me', posted_at=yesterday, expires_at=(yesterday + lifetime)),
        gen_transact('u', 'p3', 'ptype', text='digas', posted_at=now, expires_at=(now + lifetime)),
        gen_transact('u', 'p4', 'ptype', text='por favor'),
    ]
    post_dynamo.client.transact_write_items(transacts)
    post1 = post_dynamo.get_post('p1')
    post2 = post_dynamo.get_post('p2')

    # scan with cutoff of yesterday should only see the post from a week ago
    expired_posts = list(post_dynamo.generate_expired_post_pks_with_scan(yesterday.date()))
    assert len(expired_posts) == 1
    assert expired_posts[0]['partitionKey'] == post1['partitionKey']
    assert expired_posts[0]['sortKey'] == post1['sortKey']

    # scan with cutoff of today should see posts of yesterday and a week ago
    expired_posts = list(post_dynamo.generate_expired_post_pks_with_scan(now.date()))
    assert len(expired_posts) == 2
    assert expired_posts[0]['partitionKey'] == post1['partitionKey']
    assert expired_posts[0]['sortKey'] == post1['sortKey']
    assert expired_posts[1]['partitionKey'] == post2['partitionKey']
    assert expired_posts[1]['sortKey'] == post2['sortKey']


def test_set_last_unviewed_comment_at(post_dynamo):
    user_id = 'uid'
    post_id = 'pid'

    # add a post, verify starts with no new comment activity
    transact = post_dynamo.transact_add_pending_post(user_id, post_id, 'ptype', text='lore ipsum')
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert 'gsiA3PartitionKey' not in post_item
    assert 'gsiA3SortKey' not in post_item

    # add some comment activity
    at = pendulum.now('utc')
    post_item = post_dynamo.set_last_unviewed_comment_at(post_item, at)
    assert post_item['gsiA3PartitionKey'].split('/') == ['post', 'uid']
    assert pendulum.parse(post_item['gsiA3SortKey']) == at

    # update the comment activity
    at = pendulum.now('utc')
    post_item = post_dynamo.set_last_unviewed_comment_at(post_item, at)
    assert post_item['gsiA3PartitionKey'].split('/') == ['post', 'uid']
    assert pendulum.parse(post_item['gsiA3SortKey']) == at

    # clear the comment activity
    at = pendulum.now('utc')
    post_item = post_dynamo.set_last_unviewed_comment_at(post_item, None)
    assert 'gsiA3PartitionKey' not in post_item
    assert 'gsiA3SortKey' not in post_item

    # no-op: clear the comment activity again
    at = pendulum.now('utc')
    post_item = post_dynamo.set_last_unviewed_comment_at(post_item, None)
    assert 'gsiA3PartitionKey' not in post_item
    assert 'gsiA3SortKey' not in post_item


def test_transact_set_album_id_pending_post(post_dynamo):
    post_id = 'pid'

    # add a post without an album_id
    transact = post_dynamo.transact_add_pending_post('uid', post_id, 'ptype', text='lore ipsum')
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert 'albumId' not in post_item
    assert 'gsiK3PartitionKey' not in post_item
    assert 'gsiK3SortKey' not in post_item

    # verify can't specify album rank when setting the album id, since this is a pending post
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_album_id(post_item, 'aid', album_rank=0.5)

    # set the album_id, verify that worked
    transact = post_dynamo.transact_set_album_id(post_item, 'aid')
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert post_item['albumId'] == 'aid'
    assert post_item['gsiK3PartitionKey'] == 'post/aid'
    assert post_item['gsiK3SortKey'] == -1

    # verify again can't specify album rank when setting the album id, since this is a pending post
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_album_id(post_item, 'aid2', album_rank=0.5)

    # change the album id, verify that worked
    transact = post_dynamo.transact_set_album_id(post_item, 'aid2')
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert post_item['albumId'] == 'aid2'
    assert post_item['gsiK3PartitionKey'] == 'post/aid2'
    assert post_item['gsiK3SortKey'] == -1

    # verify can't specify album rank when removing the album id
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_album_id(post_item, None, album_rank=0.2)

    # remove the album id, verify that worked
    transact = post_dynamo.transact_set_album_id(post_item, None)
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert 'albumId' not in post_item
    assert 'gsiK3PartitionKey' not in post_item
    assert 'gsiK3SortKey' not in post_item


def test_transact_set_album_id_completed_post(post_dynamo):
    post_id = 'pid'

    # add a post without an album_id
    transact = post_dynamo.transact_add_pending_post('uid', post_id, 'ptype', text='lore ipsum')
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    transact = post_dynamo.transact_set_post_status(post_item, PostStatus.COMPLETED)
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert post_item['postStatus'] == PostStatus.COMPLETED
    assert 'albumId' not in post_item
    assert 'gsiK3PartitionKey' not in post_item
    assert 'gsiK3SortKey' not in post_item

    # verify t must specify album rank when setting the album id, since this is a completed post
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_album_id(post_item, 'aid')

    # set the album_id, verify that worked
    transact = post_dynamo.transact_set_album_id(post_item, 'aid', album_rank=-0.5)
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert post_item['albumId'] == 'aid'
    assert post_item['gsiK3PartitionKey'] == 'post/aid'
    assert post_item['gsiK3SortKey'] == decimal.Decimal('-0.5')

    # verify again must specify album rank when setting the album id, since this is a completed post
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_album_id(post_item, 'aid2')

    # change the album id, verify that worked
    transact = post_dynamo.transact_set_album_id(post_item, 'aid2', album_rank=0.8)
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert post_item['albumId'] == 'aid2'
    assert post_item['gsiK3PartitionKey'] == 'post/aid2'
    assert post_item['gsiK3SortKey'] == decimal.Decimal('0.8')

    # verify can't specify album rank when removing the album id
    with pytest.raises(AssertionError):
        post_dynamo.transact_set_album_id(post_item, None, album_rank=0.2)

    # remove the album id, verify that worked
    transact = post_dynamo.transact_set_album_id(post_item, None)
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert 'albumId' not in post_item
    assert 'gsiK3PartitionKey' not in post_item
    assert 'gsiK3SortKey' not in post_item


def test_transact_set_album_id_fails_wrong_status(post_dynamo):
    post_id = 'pid'

    # add a post without an album_id
    transact = post_dynamo.transact_add_pending_post('uid', post_id, 'ptype', text='lore ipsum')
    post_dynamo.client.transact_write_items([transact])
    post_item = post_dynamo.get_post(post_id)
    assert 'albumId' not in post_item
    assert 'gsiK3PartitionKey' not in post_item
    assert 'gsiK3SortKey' not in post_item

    # change the in-mem status so it doesn't match dynamo
    # verify transaction fails rather than write conflicting data to db
    post_item['postStatus'] = 'ERROR'
    transact = post_dynamo.transact_set_album_id(post_item, 'aid2')
    with pytest.raises(post_dynamo.client.exceptions.TransactionCanceledException):
        post_dynamo.client.transact_write_items([transact])

    # verify nothing changed
    post_item = post_dynamo.get_post(post_id)
    assert 'albumId' not in post_item
    assert 'gsiK3PartitionKey' not in post_item
    assert 'gsiK3SortKey' not in post_item


def test_generate_post_ids_in_album(post_dynamo):
    # generate for an empty set
    assert list(post_dynamo.generate_post_ids_in_album('aid-nope')) == []

    # add two posts in an album
    album_id = 'aid'
    post_id_1, post_id_2 = 'pid1', 'pid2'
    transacts = [
        post_dynamo.transact_add_pending_post('uid', post_id_1, 'ptype', text='lore', album_id=album_id),
        post_dynamo.transact_add_pending_post('uid', post_id_2, 'ptype', text='lore', album_id=album_id),
    ]
    post_dynamo.client.transact_write_items(transacts)

    # verify those posts do show up if we query all posts
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id))
    assert len(post_ids) == 2
    assert post_id_1 in post_ids
    assert post_id_2 in post_ids

    # verify those posts don't show up if we query COMPLETED posts
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, completed=True))
    assert len(post_ids) == 0

    # verify those posts do show up if we query non-COMPLETED posts
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, completed=False))
    assert len(post_ids) == 2
    assert post_id_1 in post_ids
    assert post_id_2 in post_ids

    # mark one post completed, another archived
    post_item_2 = post_dynamo.get_post(post_id_2)
    transacts = [post_dynamo.transact_set_post_status(post_item_2, PostStatus.COMPLETED, album_rank=0.5)]
    post_dynamo.client.transact_write_items(transacts)

    # verify both posts show up if we don't care about status
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id))
    assert len(post_ids) == 2
    assert post_id_1 in post_ids
    assert post_id_2 in post_ids

    # verify only completed post shows up if we query COMPLETED posts
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, completed=True))
    assert len(post_ids) == 1
    assert post_id_2 in post_ids

    # verify only non-completed post shows up if we query non-COMPLETED posts
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, completed=False))
    assert len(post_ids) == 1
    assert post_id_1 in post_ids

    # verify we can't combine both completed and after_rank kwargs
    with pytest.raises(AssertionError):
        post_dynamo.generate_post_ids_in_album(album_id, completed=True, after_rank=decimal.Decimal(0))

    # mark the other post completed
    post_item_1 = post_dynamo.get_post(post_id_1)
    transacts = [post_dynamo.transact_set_post_status(post_item_1, PostStatus.COMPLETED, album_rank=-0.5)]
    post_dynamo.client.transact_write_items(transacts)

    # test generating with after_rank before both
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, after_rank=decimal.Decimal(-0.625)))
    assert len(post_ids) == 2
    assert post_id_1 in post_ids
    assert post_id_2 in post_ids

    # test generating with after_rank is exclusive first one
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, after_rank=decimal.Decimal(-0.5)))
    assert len(post_ids) == 1
    assert post_id_2 in post_ids

    # test generating with after_rank between
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, after_rank=decimal.Decimal(0)))
    assert len(post_ids) == 1
    assert post_id_2 in post_ids

    # test generating with after_rank exclusive 2nd one
    post_ids = list(post_dynamo.generate_post_ids_in_album(album_id, after_rank=decimal.Decimal(0.5)))
    assert len(post_ids) == 0


def test_transact_set_album_rank(post_dynamo):
    # add a posts in an album
    album_id, post_id = 'aid', 'pid'
    transacts = [
        post_dynamo.transact_add_pending_post('uid', post_id, 'ptype', text='lore', album_id=album_id),
    ]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['gsiK3SortKey'] == -1

    # change the album rank
    transacts = [post_dynamo.transact_set_album_rank(post_id, album_rank=0)]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['gsiK3SortKey'] == 0

    # change the album rank
    transacts = [post_dynamo.transact_set_album_rank(post_id, album_rank=0.5)]
    post_dynamo.client.transact_write_items(transacts)
    post_item = post_dynamo.get_post(post_id)
    assert post_item['gsiK3SortKey'] == 0.5


def test_increment_decrement_comment_count(post_dynamo, caplog):
    post_id = str(uuid4())

    # add a post, verify starts with no comment count
    transact = post_dynamo.transact_add_pending_post(str(uuid4()), post_id, 'ptype', text='lore ipsum')
    post_dynamo.client.transact_write_items([transact])
    assert 'commentCount' not in post_dynamo.get_post(post_id)

    # verify failing hard on attempted decrement below zero
    with pytest.raises(post_dynamo.client.exceptions.ConditionalCheckFailedException):
        post_dynamo.decrement_comment_count(post_id)

    # verify failing soft on attempted decrement below zero
    with caplog.at_level(logging.WARNING):
        assert post_dynamo.decrement_comment_count(post_id, fail_soft=True) is None
    assert len(caplog.records) == 1
    assert 'Failed to decrement comment count' in caplog.records[0].msg
    assert post_id in caplog.records[0].msg

    # increment
    assert post_dynamo.increment_comment_count(post_id)['commentCount'] == 1
    assert post_dynamo.get_post(post_id)['commentCount'] == 1

    # increment again
    assert post_dynamo.increment_comment_count(post_id)['commentCount'] == 2
    assert post_dynamo.get_post(post_id)['commentCount'] == 2

    # decrement
    assert post_dynamo.decrement_comment_count(post_id)['commentCount'] == 1
    assert post_dynamo.get_post(post_id)['commentCount'] == 1


def test_transact_increment_decrement_clear_comments_unviewed_count(post_dynamo, caplog):
    post_id = str(uuid4())

    # add a post, check starting state
    transacts = [post_dynamo.transact_add_pending_post(str(uuid4()), post_id, 'ptype', text='lore ipsum')]
    post_dynamo.client.transact_write_items(transacts)
    assert 'commentsUnviewedCount' not in post_dynamo.get_post(post_id)

    # increment
    post_dynamo.increment_comment_count(post_id, viewed=False)
    assert post_dynamo.get_post(post_id)['commentsUnviewedCount'] == 1

    # increment
    post_dynamo.increment_comment_count(post_id, viewed=False)
    assert post_dynamo.get_post(post_id)['commentsUnviewedCount'] == 2

    # no change
    post_dynamo.increment_comment_count(post_id, viewed=True)
    assert post_dynamo.get_post(post_id)['commentsUnviewedCount'] == 2

    # decrement
    post_dynamo.decrement_comments_unviewed_count(post_id)
    assert post_dynamo.get_post(post_id)['commentsUnviewedCount'] == 1

    # increment
    post_dynamo.increment_comment_count(post_id, viewed=False)
    assert post_dynamo.get_post(post_id)['commentsUnviewedCount'] == 2

    # clear
    assert 'commentsUnviewedCount' not in post_dynamo.clear_comments_unviewed_count(post_id)
    assert 'commentsUnviewedCount' not in post_dynamo.get_post(post_id)

    # check clearing is idemopotent
    assert 'commentsUnviewedCount' not in post_dynamo.clear_comments_unviewed_count(post_id)
    assert 'commentsUnviewedCount' not in post_dynamo.get_post(post_id)

    # check decrement fail hard
    with pytest.raises(post_dynamo.client.exceptions.ConditionalCheckFailedException):
        post_dynamo.decrement_comments_unviewed_count(post_id)

    # check decrement fail soft
    with caplog.at_level(logging.WARNING):
        assert post_dynamo.decrement_comments_unviewed_count(post_id, fail_soft=True) is None
    assert len(caplog.records) == 1
    assert 'Failed to decrement comments unviewed count' in caplog.records[0].msg
    assert post_id in caplog.records[0].msg


@pytest.mark.parametrize(
    'incrementor_name, decrementor_name, attribute_name',
    [['increment_flag_count', 'decrement_flag_count', 'flagCount']],
)
def test_increment_decrement_count(post_dynamo, caplog, incrementor_name, decrementor_name, attribute_name):
    incrementor = getattr(post_dynamo, incrementor_name)
    decrementor = getattr(post_dynamo, decrementor_name)

    # add the post to the DB, verify it is in DB
    post_id = str(uuid4())
    transact = post_dynamo.transact_add_pending_post(str(uuid4()), post_id, 'ptype')
    post_dynamo.client.transact_write_items([transact])
    assert attribute_name not in post_dynamo.get_post(post_id)

    # verify can't decrement below zero
    with pytest.raises(post_dynamo.client.exceptions.ConditionalCheckFailedException):
        decrementor(post_id)
    assert attribute_name not in post_dynamo.get_post(post_id)

    # increment
    assert incrementor(post_id)[attribute_name] == 1
    assert post_dynamo.get_post(post_id)[attribute_name] == 1

    # increment
    assert incrementor(post_id)[attribute_name] == 2
    assert post_dynamo.get_post(post_id)[attribute_name] == 2

    # decrement
    assert decrementor(post_id)[attribute_name] == 1
    assert post_dynamo.get_post(post_id)[attribute_name] == 1

    # decrement
    assert decrementor(post_id)[attribute_name] == 0
    assert post_dynamo.get_post(post_id)[attribute_name] == 0

    # verify fail soft on trying to decrement below zero
    with caplog.at_level(logging.WARNING):
        resp = decrementor(post_id, fail_soft=True)
    assert resp is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == 'WARNING'
    assert all(x in caplog.records[0].msg for x in ['Failed to decrement', attribute_name, post_id])
    assert post_dynamo.get_post(post_id)[attribute_name] == 0

    # verify fail hard on trying to decrement below zero
    with pytest.raises(post_dynamo.client.exceptions.ConditionalCheckFailedException):
        decrementor(post_id)
    assert post_dynamo.get_post(post_id)[attribute_name] == 0
